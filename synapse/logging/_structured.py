# -*- coding: utf-8 -*-
# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os.path
import sys

import attr
from constantly import NamedConstant, Names, ValueConstant, Values
from zope.interface import implementer

from twisted.logger import (
    FileLogObserver,
    FilteringLogObserver,
    ILogObserver,
    Logger,
    LogLevel,
    LogLevelFilterPredicate,
    LogPublisher,
    eventAsText,
    globalLogBeginner,
    jsonFileLogObserver,
)

from synapse.config._base import ConfigError
from synapse.logging._fluentd import FluentDToConsoleLogObserver
from synapse.logging.context import LoggingContext


def stdlib_log_level_to_twisted(level):
    """
    Convert a stdlib log level to Twisted's log level.
    """
    lvl = level.lower().replace("warning", "warn")
    return LogLevel.levelWithName(lvl)


@attr.s
@implementer(ILogObserver)
class LogContextObserver(object):
    """
    An ILogObserver which adds Synapse-specific log context information.

    Attributes:
        observer (ILogObserver): The target parent observer.
    """

    observer = attr.ib()

    def __call__(self, event):
        """
        Consume a log event and emit it to the parent observer after filtering
        and adding log context information.

        Args:
            event (dict)
        """
        # Filter out some useless events that Twisted outputs
        if "log_text" in event:
            if event["log_text"].startswith("DNSDatagramProtocol starting on "):
                return

            if event["log_text"].startswith("(UDP Port "):
                return

            if event["log_text"].startswith("Timing out client") or event[
                "log_format"
            ].startswith("Timing out client"):
                return

        context = LoggingContext.current_context()

        # Copy the context information to the log event.
        if context is not None:
            context.copy_to_twisted_log_entry(event)
        else:
            # If there's no logging context, not even the root one, we might be
            # starting up or it might be from non-Synapse code. Log it as if it
            # came from the root logger.
            event["request"] = None
            event["scope"] = None

        return self.observer(event)


class PythonStdlibToTwistedLogger(logging.Handler):
    """
    Transform a Python stdlib log message into a Twisted one.
    """

    def __init__(self, observer, *args, **kwargs):
        """
        Args:
            observer (ILogObserver): A Twisted logging observer.
            *args, **kwargs: Args/kwargs to be passed to logging.Handler.
        """
        self.observer = observer
        super().__init__(*args, **kwargs)

    def emit(self, record):
        """
        Emit a record to Twisted's observer.

        Args:
            record (logging.LogRecord)
        """

        self.observer(
            {
                "log_time": record.created,
                "log_text": record.getMessage(),
                "log_format": "{log_text}",
                "log_namespace": record.name,
                "log_level": stdlib_log_level_to_twisted(record.levelname),
            }
        )


def SynapseFileLogObserver(outFile):
    """
    A log observer that formats events like the traditional log formatter and
    sends them to `outFile`.

    Args:
        outFile (file object): The file object to write to.
    """

    def formatEvent(_event):
        event = dict(_event)
        event["log_level"] = event["log_level"].name.upper()
        event["log_format"] = "- {log_namespace} - {log_level} - {request} - " + (
            event.get("log_format", "{log_text}") or "{log_text}"
        )
        return eventAsText(event, includeSystem=False) + "\n"

    return FileLogObserver(outFile, formatEvent)


class DrainType(Names):
    CONSOLE = NamedConstant()
    CONSOLE_JSON = NamedConstant()
    FILE = NamedConstant()
    FILE_JSON = NamedConstant()
    FLUENTD = NamedConstant()


class OutputPipeType(Values):
    stdout = ValueConstant(sys.__stdout__)
    stderr = ValueConstant(sys.__stderr__)


@attr.s
class DrainConfiguration(object):
    name = attr.ib()
    type = attr.ib()
    location = attr.ib()


DEFAULT_LOGGERS = {"synapse": {"level": "INFO"}}


def parse_drain_configs(drains):
    """
    Parse the drain configurations.

    Args:
        drains (list): A list of drain configurations.

    Yields:
        DrainConfiguration instances.

    Raises:
        ConfigError: If any of the drain configuration items are invalid.
    """
    for name, config in drains.items():
        if "type" not in config:
            raise ConfigError("Logging drains require a 'type' key.")

        try:
            logging_type = DrainType.lookupByName(config["type"].upper())
        except ValueError:
            raise ConfigError(
                "%s is not a known logging drain type." % (config["type"],)
            )

        if logging_type in [
            DrainType.CONSOLE,
            DrainType.CONSOLE_JSON,
            DrainType.FLUENTD,
        ]:
            location = config.get("location")
            if location is None or location not in ["stdout", "stderr"]:
                raise ConfigError(
                    (
                        "The %s drain needs the 'location' key set to "
                        "either 'stdout' or 'stderr'."
                    )
                    % (logging_type,)
                )

            pipe = OutputPipeType.lookupByName(location).value

            yield DrainConfiguration(name=name, type=logging_type, location=pipe)

        elif logging_type in [DrainType.FILE, DrainType.FILE_JSON]:
            if "location" not in config:
                raise ConfigError(
                    "The %s drain needs the 'location' key set." % (logging_type,)
                )

            location = config.get("location")
            if os.path.abspath(location) != location:
                raise ConfigError(
                    "File paths need to be absolute, '%s' is a relative path"
                    % (location,)
                )
            yield DrainConfiguration(name=name, type=logging_type, location=location)

        else:
            raise ConfigError(
                "The %s drain type is currently not implemented."
                % (config["type"].upper(),)
            )


def setup_structured_logging(config, log_config, logBeginner=globalLogBeginner):
    """
    Set up Twisted's structured logging system.

    Args:
        config (HomeserverConfig): The configuration of the Synapse homeserver.
        log_config (dict): The log configuration to use.
    """
    if config.no_redirect_stdio:
        raise ConfigError(
            "no_redirect_stdio cannot be defined using structured logging."
        )

    logger = Logger()

    if "drains" not in log_config:
        raise ConfigError("The logging configuration requires a list of drains.")

    observers = []

    for observer in parse_drain_configs(log_config["drains"]):
        # Pipe drains
        if observer.type == DrainType.CONSOLE:
            logger.debug(
                "Starting up the {name} console logger drain", name=observer.name
            )
            observers.append(SynapseFileLogObserver(observer.location))
        if observer.type == DrainType.CONSOLE_JSON:
            logger.debug(
                "Starting up the {name} JSON console logger drain", name=observer.name
            )
            observers.append(jsonFileLogObserver(observer.location))

        # File drains
        if observer.type == DrainType.FILE:
            logger.debug("Starting up the {name} file logger drain", name=observer.name)
            log_file = open(observer.location, "at", buffering=1, encoding="utf8")
            observers.append(SynapseFileLogObserver(log_file))
        if observer.type == DrainType.FILE_JSON:
            logger.debug(
                "Starting up the {name} JSON file logger drain", name=observer.name
            )
            log_file = open(observer.location, "at", buffering=1, encoding="utf8")
            observers.append(jsonFileLogObserver(log_file))

        if observer.type == DrainType.FLUENTD:
            logger.debug(
                "Starting up the {name} FluentD console logger drain",
                name=observer.name,
            )
            observers.append(FluentDToConsoleLogObserver(observer.location))

    publisher = LogPublisher(*observers)
    log_filter = LogLevelFilterPredicate()

    for namespace, namespace_config in log_config.get(
        "loggers", DEFAULT_LOGGERS
    ).items():
        # Set the log level for twisted.logger.Logger namespaces
        log_filter.setLogLevelForNamespace(
            namespace,
            stdlib_log_level_to_twisted(namespace_config.get("level", "INFO")),
        )

        # Also set the log levels for the stdlib logger namespaces, to prevent
        # them getting to PythonStdlibToTwistedLogger and having to be formatted
        if "level" in namespace_config:
            logging.getLogger(namespace).setLevel(namespace_config.get("level"))

    f = FilteringLogObserver(publisher, [log_filter])
    lco = LogContextObserver(f)
    stuff_into_twisted = PythonStdlibToTwistedLogger(lco)

    stdliblogger = logging.getLogger("")
    stdliblogger.addHandler(stuff_into_twisted)

    # Always redirect standard I/O, otherwise other logging outputs might miss
    # it.
    logBeginner.beginLoggingTo([lco], redirectStandardIO=True)

    return publisher


def reload_structured_logging(*args, log_config=None):
    # TODO: Reload the structured logging system. Since we don't implement any
    # sort of file rotation, we don't need to worry about doing that here.
    pass
