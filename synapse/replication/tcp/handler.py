# -*- coding: utf-8 -*-
# Copyright 2017 Vector Creations Ltd
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
"""A replication client for use by synapse workers.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Set

from prometheus_client import Counter

from synapse.replication.tcp.client import ReplicationClientFactory
from synapse.replication.tcp.commands import (
    Command,
    FederationAckCommand,
    InvalidateCacheCommand,
    PositionCommand,
    RdataCommand,
    RemoteServerUpCommand,
    RemovePusherCommand,
    SyncCommand,
    UserIpCommand,
    UserSyncCommand,
)
from synapse.replication.tcp.streams import STREAMS_MAP, Stream
from synapse.util.async_helpers import Linearizer

logger = logging.getLogger(__name__)


# number of updates received for each RDATA stream
inbound_rdata_count = Counter(
    "synapse_replication_tcp_protocol_inbound_rdata_count", "", ["stream_name"]
)


class ReplicationCommandHandler:
    """Handles incoming commands from replication.
    """

    def __init__(self, hs):
        self.replication_data_handler = hs.get_replication_data_handler()
        self.presence_handler = hs.get_presence_handler()

        # Set of streams that we're currently catching up with.
        self.streams_connecting = set()  # type: Set[str]

        self.streams = {
            stream.NAME: stream(hs) for stream in STREAMS_MAP.values()
        }  # type: Dict[str, Stream]

        self._position_linearizer = Linearizer("replication_position")

        # Map of stream to batched updates. See RdataCommand for info on how
        # batching works.
        self.pending_batches = {}  # type: Dict[str, List[Any]]

        # The factory used to create connections.
        self.factory = None  # type: Optional[ReplicationClientFactory]

        # The currently connected connections.
        self.connections = []

    def start_replication(self, hs):
        """Helper method to start a replication connection to the remote server
        using TCP.
        """
        client_name = hs.config.worker_name
        self.factory = ReplicationClientFactory(hs, client_name, self)
        host = hs.config.worker_replication_host
        port = hs.config.worker_replication_port
        hs.get_reactor().connectTCP(host, port, self.factory)

    async def on_RDATA(self, cmd: RdataCommand):
        stream_name = cmd.stream_name
        inbound_rdata_count.labels(stream_name).inc()

        try:
            row = STREAMS_MAP[stream_name].parse_row(cmd.row)
        except Exception:
            logger.exception("Failed to parse RDATA: %r %r", stream_name, cmd.row)
            raise

        if cmd.token is None or stream_name in self.streams_connecting:
            # I.e. this is part of a batch of updates for this stream. Batch
            # until we get an update for the stream with a non None token
            self.pending_batches.setdefault(stream_name, []).append(row)
        else:
            # Check if this is the last of a batch of updates
            rows = self.pending_batches.pop(stream_name, [])
            rows.append(row)
            await self.on_rdata(stream_name, cmd.token, rows)

    async def on_rdata(self, stream_name: str, token: int, rows: list):
        """Called to handle a batch of replication data with a given stream token.

        Args:
            stream_name: name of the replication stream for this batch of rows
            token: stream token for this batch of rows
            rows: a list of Stream.ROW_TYPE objects as returned by
                Stream.parse_row.
        """
        logger.debug("Received rdata %s -> %s", stream_name, token)
        await self.replication_data_handler.on_rdata(stream_name, token, rows)

    async def on_POSITION(self, cmd: PositionCommand):
        stream = self.streams.get(cmd.stream_name)
        if not stream:
            logger.error("Got POSITION for unknown stream: %s", cmd.stream_name)
            return

        # We're about to go and catch up with the stream, so mark as connecting
        # to stop RDATA being handled at the same time.
        self.streams_connecting.add(cmd.stream_name)

        # We protect catching up with a linearizer in case the replicaiton
        # connection reconnects under us.
        with await self._position_linearizer.queue(cmd.stream_name):
            # Find where we previously streamed up to.
            current_token = self.replication_data_handler.get_streams_to_replicate().get(
                cmd.stream_name
            )
            if current_token is None:
                logger.warning(
                    "Got POSITION for stream we're not subscribed to: %s",
                    cmd.stream_name,
                )
                return

            # Fetch all updates between then and now.
            limited = True
            while limited:
                updates, current_token, limited = await stream.get_updates_since(
                    current_token, cmd.token
                )
                if updates:
                    await self.on_rdata(
                        cmd.stream_name,
                        current_token,
                        [stream.parse_row(update[1]) for update in updates],
                    )

            # We've now caught up to position sent to us, notify handler.
            await self.replication_data_handler.on_position(cmd.stream_name, cmd.token)

        self.streams_connecting.discard(cmd.stream_name)

        # Handle any RDATA that came in while we were catching up.
        rows = self.pending_batches.pop(cmd.stream_name, [])
        if rows:
            await self.on_rdata(cmd.stream_name, rows[-1].token, rows)

    async def on_SYNC(self, cmd: SyncCommand):
        pass

    async def on_REMOTE_SERVER_UP(self, cmd: RemoteServerUpCommand):
        """"Called when get a new REMOTE_SERVER_UP command."""

    def get_currently_syncing_users(self):
        """Get the list of currently syncing users (if any). This is called
        when a connection has been established and we need to send the
        currently syncing users. (Overriden by the synchrotron's only)
        """
        return self.presence_handler.get_currently_syncing_users()

    def new_connection(self, connection):
        self.connections.append(connection)

        # If we're using a ReplicationClientFactory then we reset the connection
        # delay now.
        if self.factory:
            self.factory.resetDelay()

    def lost_connection(self, connection):
        try:
            self.connections.remove(connection)
        except ValueError:
            pass

    def connected(self) -> bool:
        """Do we have any replication connections open?

        Used to no-op if nothing is connected.
        """
        return bool(self.connections)

    def send_command(self, cmd: Command):
        """Send a command to master (when we get establish a connection if we
        don't have one already.)
        """
        if self.connections:
            for connection in self.connections:
                connection.send_command(cmd)
        else:
            logger.warning("Dropping command as not connected: %r", cmd.NAME)

    def send_federation_ack(self, token: int):
        """Ack data for the federation stream. This allows the master to drop
        data stored purely in memory.
        """
        self.send_command(FederationAckCommand(token))

    def send_user_sync(
        self, instance_id: str, user_id: str, is_syncing: bool, last_sync_ms: int
    ):
        """Poke the master that a user has started/stopped syncing.
        """
        self.send_command(
            UserSyncCommand(instance_id, user_id, is_syncing, last_sync_ms)
        )

    def send_remove_pusher(self, app_id: str, push_key: str, user_id: str):
        """Poke the master to remove a pusher for a user
        """
        cmd = RemovePusherCommand(app_id, push_key, user_id)
        self.send_command(cmd)

    def send_invalidate_cache(self, cache_func: Callable, keys: tuple):
        """Poke the master to invalidate a cache.
        """
        cmd = InvalidateCacheCommand(cache_func.__name__, keys)
        self.send_command(cmd)

    def send_user_ip(
        self,
        user_id: str,
        access_token: str,
        ip: str,
        user_agent: str,
        device_id: str,
        last_seen: int,
    ):
        """Tell the master that the user made a request.
        """
        cmd = UserIpCommand(user_id, access_token, ip, user_agent, device_id, last_seen)
        self.send_command(cmd)

    def send_remote_server_up(self, server: str):
        self.send_command(RemoteServerUpCommand(server))
