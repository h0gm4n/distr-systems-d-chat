import asyncio
import logging
import random
from typing import Any, Dict, List, Callable, Optional, Protocol


class PeerProvider(Protocol):
    """Protocol for peer discovery - allows dynamic peer refresh."""
    def peers(self) -> List[str]:
        ...

from message_protocol import encode_msg, decode_msg

logger = logging.getLogger("raft")


class LogEntry:
    def __init__(self, term: int, command: Dict[str, Any]):
        self.term = term
        self.command = command

    def to_dict(self) -> Dict[str, Any]:
        return {"term": self.term, "command": self.command}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "LogEntry":
        return LogEntry(term=d["term"], command=d["command"])


class RaftNode:
    """Very simplified RAFT node.

    - Uses a separate TCP port for RAFT RPCs (AppendEntries, RequestVote)
    - Each RPC opens a short-lived connection to the peer.
    - Heartbeats are empty AppendEntries.
    - On client command, leader appends entry then replicates to followers;
      commits when majority acknowledges.
    """

    def __init__(
        self,
        node_id: str,
        host: str,
        raft_port: int,
        peers: List[str],
        apply_callback: Callable[[Dict[str, Any]], asyncio.Future],
    ):
        self.node_id = node_id
        self.host = host
        self.raft_port = raft_port
        self.peers = peers  # list of "host:port"

        # persistent state
        self.current_term = 0
        self.voted_for: Optional[str] = None
        self.log: List[LogEntry] = []

        # volatile
        self.commit_index = -1
        self.last_applied = -1

        # leader state
        self.next_index: Dict[str, int] = {}
        self.match_index: Dict[str, int] = {}

        # role
        self.state = "follower"  # follower | candidate | leader
        self.leader_id: Optional[str] = None  # track current known leader id

        # timers
        self.election_timeout = self._random_timeout()
        self.last_heartbeat = asyncio.get_event_loop().time()
        self.heartbeat_interval = 1.0

        # election tracking
        self._failed_elections = 0

        self.apply_callback = apply_callback
        self._server: Optional[asyncio.base_events.Server] = None
        self._lock = asyncio.Lock()
        
        # optional peer provider for dynamic peer refresh
        self._peer_provider: Optional[Callable[[], List[str]]] = None
        self._peer_refresh_interval = 30.0  # refresh peers every 30 seconds
        self._last_peer_refresh = 0.0

    def _random_timeout(self) -> float:
        # Broader, longer timeout to avoid dueling elections in AWS
        return random.uniform(5.0, 10.0)

    def set_peer_provider(self, provider: PeerProvider) -> None:
        """Set a peer provider for dynamic peer discovery."""
        self._peer_provider = provider.peers
        
    def _refresh_peers_if_needed(self) -> None:
        """Refresh peer list from provider if interval has elapsed."""
        if self._peer_provider is None:
            logger.info("OBS! _refresh_peers_if_needed() called but no peer provider set")
            return
            
        now = asyncio.get_event_loop().time()
        if now - self._last_peer_refresh >= self._peer_refresh_interval:
            try:
                new_peers = self._peer_provider()
                if set(new_peers) != set(self.peers):
                    logger.info(
                        "%s: Peer list updated: %s -> %s",
                        self.node_id, self.peers, new_peers
                    )
                self.peers = new_peers
                self._last_peer_refresh = now

                logger.debug("Peers updated: %s", self.peers)
            except Exception as e:
                logger.warning("%s: Failed to refresh peers: %s", self.node_id, e)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_peer_connection, self.host, self.raft_port
        )
        logger.info(
            "%s RAFT listening on %s:%d (%s)",
            self.node_id,
            self.host,
            self.raft_port,
            self.state,
        )
        asyncio.create_task(self._election_loop())

    async def _handle_peer_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle inbound RAFT RPCs."""
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                msg = decode_msg(line.decode().strip())
                mtype = msg.get("type")
                if mtype == "RequestVote":
                    resp = await self._handle_request_vote(msg)
                elif mtype == "AppendEntries":
                    resp = await self._handle_append_entries(msg)
                else:
                    resp = {"error": "unknown_rpc"}
                writer.write(encode_msg(resp))
                await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # === RAFT RPC handlers ===

    async def _handle_request_vote(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            term = msg.get("term", 0)
            candidate_id = msg.get("candidate_id")
            last_log_index = msg.get("last_log_index", -1)
            last_log_term = msg.get("last_log_term", 0)

            logger.info(
                "%s: Received RequestVote from %s (term=%d, last_log_index=%d, last_log_term=%d)",
                self.node_id, candidate_id, term, last_log_index, last_log_term
            )

            if term < self.current_term:
                logger.info(
                    "%s: Rejecting RequestVote from %s (term=%d < current_term=%d)",
                    self.node_id, candidate_id, term, self.current_term
                )
                return {
                    "type": "RequestVoteResponse",
                    "term": self.current_term,
                    "vote_granted": False,
                }

            if term > self.current_term:
                self.current_term = term
                self.voted_for = None
                self.state = "follower"

            # check if candidate's log is at least as up-to-date
            my_last_index = len(self.log) - 1
            my_last_term = self.log[my_last_index].term if self.log else 0

            up_to_date = (last_log_term > my_last_term) or (
                last_log_term == my_last_term and last_log_index >= my_last_index
            )

            if (self.voted_for in (None, candidate_id)) and up_to_date:
                self.voted_for = candidate_id
                self.last_heartbeat = asyncio.get_event_loop().time()
                logger.info(
                    "%s: Granting vote to %s for term %d",
                    self.node_id, candidate_id, self.current_term
                )
                return {
                    "type": "RequestVoteResponse",
                    "term": self.current_term,
                    "vote_granted": True,
                }

            logger.info(
                "%s: Not granting vote to %s (voted_for=%s, up_to_date=%s)",
                self.node_id, candidate_id, self.voted_for, up_to_date
            )
            return {
                "type": "RequestVoteResponse",
                "term": self.current_term,
                "vote_granted": False,
            }


    async def _handle_append_entries(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            term = msg.get("term", 0)
            leader_id = msg.get("leader_id")
            prev_log_index = msg.get("prev_log_index", -1)
            prev_log_term = msg.get("prev_log_term", 0)
            entries = msg.get("entries", [])
            leader_commit = msg.get("leader_commit", -1)

            # Track what changed for smarter logging
            old_leader = self.leader_id
            old_commit_index = self.commit_index
            old_term = self.current_term

            # Only log at INFO level if something significant is happening
            if entries or term != old_term or leader_id != old_leader:
                logger.info(
                    "%s: Received AppendEntries from %s (term=%d, prev_log_index=%d, entries=%d, leader_commit=%d)",
                    self.node_id, leader_id, term, prev_log_index, len(entries), leader_commit
                )
            else:
                logger.debug(
                    "%s: Received heartbeat from %s (term=%d, leader_commit=%d)",
                    self.node_id, leader_id, term, leader_commit
                )

            if term < self.current_term:
                logger.info(
                    "%s: Rejecting AppendEntries from %s (term=%d < current_term=%d)",
                    self.node_id, leader_id, term, self.current_term
                )
                return {
                    "type": "AppendEntriesResponse",
                    "term": self.current_term,
                    "success": False,
                }

            # Valid AppendEntries from current or newer term - update state
            self.current_term = term
            self.state = "follower"
            self.leader_id = leader_id  # Track who the leader is
            self.last_heartbeat = asyncio.get_event_loop().time()

            # Log leader recognition only when leader changes
            if leader_id != old_leader:
                logger.info(
                    "%s: Recognized %s as leader for term %d",
                    self.node_id, leader_id, term
                )

            # check log consistency
            if prev_log_index >= 0:
                if prev_log_index >= len(self.log):
                    logger.info(
                        "%s: AppendEntries log inconsistency (prev_log_index=%d >= len(log)=%d)",
                        self.node_id, prev_log_index, len(self.log)
                    )
                    return {
                        "type": "AppendEntriesResponse",
                        "term": self.current_term,
                        "success": False,
                    }
                if self.log[prev_log_index].term != prev_log_term:
                    logger.info(
                        "%s: AppendEntries term mismatch at index %d (expected_term=%d, got=%d) -> truncating",
                        self.node_id, prev_log_index, self.log[prev_log_index].term, prev_log_term
                    )
                    # conflict: delete entry and all that follow it
                    self.log = self.log[: prev_log_index]
                    return {
                        "type": "AppendEntriesResponse",
                        "term": self.current_term,
                        "success": False,
                    }

            # append any new entries
            for entry_dict in entries:
                self.log.append(LogEntry.from_dict(entry_dict))

            commit_changed = False

            if leader_commit > self.commit_index:
                self.commit_index = min(leader_commit, len(self.log) - 1)
                commit_changed = True
                await self._apply_committed()

            # Only log success at INFO level if something meaningful happened
            if entries or commit_changed or term != old_term:
                logger.info(
                    "%s: AppendEntries from %s succeeded (new_log_len=%d, commit_index=%d)",
                    self.node_id, leader_id, len(self.log), self.commit_index
                )

            return {
                "type": "AppendEntriesResponse",
                "term": self.current_term,
                "success": True,
                "match_index": len(self.log) - 1,
            }


    # === client command from HTTP layer ===

    async def handle_client_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            if self.state != "leader":
                # Not the leader: tell client that and include the best-known leader id
                return {"status": "not_leader", "leader": self.leader_id}

            # append entry to our own log
            entry = LogEntry(term=self.current_term, command=command)
            self.log.append(entry)
            new_index = len(self.log) - 1

        # replicate to followers
        success_count = 1  # leader itself
        tasks = [self._replicate_to_peer(peer) for peer in self.peers]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, dict) and r.get("success"):
                    success_count += 1

        if success_count > (len(self.peers) + 1) // 2:
            async with self._lock:
                self.commit_index = new_index
                await self._apply_committed()
            return {"status": "ok", "index": new_index}
        else:
            return {"status": "failed"}

    async def _replicate_to_peer(self, peer: str) -> Dict[str, Any]:
        host, port_s = peer.split(":")
        port = int(port_s)

        prev_log_index = len(self.log) - 2
        prev_log_term = self.log[prev_log_index].term if prev_log_index >= 0 else 0
        entries = [self.log[-1].to_dict()] if self.log else []

        msg = {
            "type": "AppendEntries",
            "term": self.current_term,
            "leader_id": self.node_id,
            "prev_log_index": prev_log_index,
            "prev_log_term": prev_log_term,
            "entries": entries,
            "leader_commit": self.commit_index,
        }

        try:
            logger.info(
                "%s: Sending AppendEntries to %s:%d (term=%d, prev_log_index=%d, entries=%d, commit_index=%d)",
                self.node_id, host, port, self.current_term, prev_log_index, len(entries), self.commit_index
            )
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.write(encode_msg(msg))
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            if not line:
                logger.warning(
                    "%s: Empty response to AppendEntries from %s:%d",
                    self.node_id, host, port
                )
                return {"success": False}

            resp = decode_msg(line.decode().strip())
            logger.info(
                "%s: Received AppendEntriesResponse from %s:%d -> %s",
                self.node_id, host, port, resp
            )
            return resp

        except asyncio.TimeoutError:
            logger.warning(
                "%s: Timeout sending AppendEntries to %s:%d",
                self.node_id, host, port
            )
            return {"success": False}
        except Exception as e:
            logger.warning(
                "%s: Error sending AppendEntries to %s:%d: %s",
                self.node_id, host, port, e
            )
            return {"success": False}



    # === background tasks ===

    async def _election_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(0.2)
            now = loop.time()
            
            # Periodically refresh peers from provider (if set)
            self._refresh_peers_if_needed()
            
            if self.state == "leader":
                # send heartbeats
                await self._send_heartbeats()
                continue

            # follower or candidate: check election timeout
            if now - self.last_heartbeat > self.election_timeout:
                await self._start_election()
                self.election_timeout = self._random_timeout()

    async def _start_election(self) -> None:
        async with self._lock:
            self.state = "candidate"
            self.current_term += 1
            self.voted_for = self.node_id
            self.leader_id = None  # Clear leader
            term_started = self.current_term
            votes = 1

            last_index = len(self.log) - 1
            last_term = self.log[last_index].term if self.log else 0
            
            logger.info(
                "%s: Starting election for term %d",
                self.node_id, term_started
            )

        # send RequestVote to all peers
        tasks = []

        for peer in self.peers:
            host, port_s = peer.split(":")
            port = int(port_s)
            msg = {
                "type": "RequestVote",
                "term": term_started,
                "candidate_id": self.node_id,
                "last_log_index": last_index,
                "last_log_term": last_term,
            }
            tasks.append(self._send_request_vote(host, port, msg))

        max_term_from_responses = term_started

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, dict):
                    resp_term = r.get("term", 0)
                    if resp_term > max_term_from_responses:
                        max_term_from_responses = resp_term
                    if r.get("vote_granted"):
                        votes += 1

        async with self._lock:
            # If learned about a higher term, update and step down
            if max_term_from_responses > self.current_term:
                logger.info(
                    "%s: Saw higher term %d in RequestVote responses (current_term=%d), stepping down",
                    self.node_id, max_term_from_responses, self.current_term
                )
                self.current_term = max_term_from_responses
                self.voted_for = None
                self.state = "follower"
                return

            if self.current_term != term_started:
                # term changed due to newer RPCs; abort
                return

            majority = (len(self.peers) + 1) // 2

            if votes > majority:
                logger.info(
                    "%s became LEADER for term %d (votes=%d)",
                    self.node_id, self.current_term, votes
                )
                self.state = "leader"
                self.leader_id = self.node_id
                self.last_heartbeat = asyncio.get_event_loop().time()
                self._failed_elections = 0
                
                # Immediately send heartbeats to establish leadership
                asyncio.create_task(self._send_heartbeats())
            else:
                self._failed_elections += 1
                logger.info(
                    "%s failed to win election for term %d (votes=%d, failed_elections=%d)",
                    self.node_id, term_started, votes, self._failed_elections
                )
                self.state = "follower"


    async def _send_request_vote(self, host: str, port: int, msg: Dict[str, Any]) -> Dict[str, Any]:
        try:
            logger.info(
                "%s: Sending RequestVote to %s:%d (term=%d)",
                self.node_id, host, port, msg.get("term", 0)
            )
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.write(encode_msg(msg))
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            if not line:
                logger.warning(
                    "%s: Empty response to RequestVote from %s:%d",
                    self.node_id, host, port
                )
                return {"vote_granted": False}

            resp = decode_msg(line.decode().strip())
            logger.info(
                "%s: Received RequestVoteResponse from %s:%d -> %s",
                self.node_id, host, port, resp
            )
            return resp

        except asyncio.TimeoutError:
            logger.warning(
                "%s: Timeout sending RequestVote to %s:%d",
                self.node_id, host, port
            )
            return {"vote_granted": False}
        except Exception as e:
            logger.warning(
                "%s: Error sending RequestVote to %s:%d: %s",
                self.node_id, host, port, e
            )
            return {"vote_granted": False}


    async def _send_heartbeats(self) -> None:
        # leader sends empty AppendEntries as heartbeat
        tasks = []
        for peer in self.peers:
            host, port_s = peer.split(":")
            port = int(port_s)
            msg = {
                "type": "AppendEntries",
                "term": self.current_term,
                "leader_id": self.node_id,
                "prev_log_index": len(self.log) - 1,
                "prev_log_term": self.log[-1].term if self.log else 0,
                "entries": [],
                "leader_commit": self.commit_index,
            }
            tasks.append(self._send_append_entries(host, port, msg))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_append_entries(self, host: str, port: int, msg: Dict[str, Any]) -> Dict[str, Any]:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(encode_msg(msg))
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            if not line:
                return {"success": False}
            resp = decode_msg(line.decode().strip())
            return resp
        except Exception:
            return {"success": False}

    async def _apply_committed(self) -> None:
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.log[self.last_applied]
            try:
                await self.apply_callback(entry.command)
            except Exception:
                logger.exception("Failed to apply command")

    # === Debug helpers ===

    def get_all_node_ids(self) -> List[str]:
        """Return list of all node IDs in the cluster (self + peers)."""
        node_ids = [self.node_id]
        for peer in self.peers:
            # Peers are stored as "host:port", extract a readable ID
            # For AWS: private IPs like "10.0.1.5:10000"
            # For local: "127.0.0.1:10001" etc.
            node_ids.append(peer)
        return node_ids

    def get_leader_id(self) -> Optional[str]:
        """Return the current known leader ID, or None if unknown."""
        return self.leader_id

    def is_leader(self) -> bool:
        """Return True if this node is the current leader."""
        return self.state == "leader"

    def is_election_ongoing(self) -> bool:
        """Return True if an election is currently in progress."""
        return self.state == "candidate" or self.leader_id is None
