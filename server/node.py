import argparse
import asyncio
import json
import logging
import os
import sys
from typing import List

import requests

from raft import RaftNode
from state_machine import ChatState
from discovery import build_peer_provider_from_env

DCHAT_PUBLIC_HOST = os.environ.get("DCHAT_PUBLIC_HOST")  # e.g. "my-alb-1234.elb.amazonaws.com"
DCHAT_PUBLIC_SCHEME = os.environ.get("DCHAT_PUBLIC_SCHEME", "http")
DCHAT_RAFT_LOG_LEVEL = os.environ.get("DCHAT_RAFT_LOG_LEVEL", "DEBUG").upper()

MAX_MESSAGE_LENGTH = 256

# Local dev mapping: RAFT leader id -> HTTP port
LOCAL_LEADER_HTTP_PORTS = {
    "node0": 9000,
    "node1": 9001,
    "node2": 9002,
}

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
logging.getLogger("raft").setLevel(getattr(logging, DCHAT_RAFT_LOG_LEVEL, logging.DEBUG))

logger = logging.getLogger("node")


async def run_node(node_id: str, http_port: int, raft_port: int, peers: List[str], peer_provider=None) -> None:
    state = ChatState(path=f"chat_log_{http_port}.jsonl")

    async def apply_callback(command):
        await state.apply(command)

    raft = RaftNode(
        node_id=node_id,
        host="0.0.0.0",
        raft_port=raft_port,
        peers=peers,
        apply_callback=apply_callback,
    )
    
    # Enable dynamic peer refresh if provider is available
    if peer_provider is not None:
        raft.set_peer_provider(peer_provider)
        logger.info("Dynamic peer discovery enabled for %s", node_id)
    else:
        logger.info("OBS! Dynamic peer discovery disabled for %s", node_id)
        logger.info("run_node() never received a peer_provider")

    await raft.start()
    asyncio.create_task(start_http_server(http_port, raft, state))



async def start_http_server(port: int, raft: RaftNode, state: ChatState) -> None:
    """Very small HTTP server for /health, /chat, /messages."""

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return

        header_text = data.decode(errors="ignore")
        lines = header_text.split("\r\n")
        if not lines:
            writer.close()
            return

        first_line = lines[0]
        parts = first_line.split()
        if len(parts) < 2:
            writer.close()
            return
        method, path = parts[0], parts[1]

        # parse headers we care about
        content_length = 0
        host_header = None
        forwarded_proto = None

        for line in lines[1:]:
            lower = line.lower()
            if lower.startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except Exception:
                    content_length = 0
            elif lower.startswith("host:"):
                host_header = line.split(":", 1)[1].strip()
            elif lower.startswith("x-forwarded-proto:"):
                forwarded_proto = line.split(":", 1)[1].strip().lower()

        if not host_header:
            # local dev fallback
            host_header = f"127.0.0.1:{port}"

        scheme = forwarded_proto or "http"

        body = b""
        if content_length > 0:
            try:
                body = await reader.readexactly(content_length)
            except Exception:
                body = b""

        # --- simple endpoints ---

        if path == "/health":
            resp_body = b"OK"
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                + resp_body
            )
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        if path == "/messages" and method == "GET":
            msgs = state.all_messages()
            resp_body = json.dumps(msgs).encode()
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                + resp_body
            )
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        # --- debug endpoints ---

        if path == "/instances" and method == "GET":
            nodes = raft.get_all_node_ids()
            resp_body = json.dumps({"nodes": nodes}).encode()
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                + resp_body
            )
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        if path == "/leader" and method == "GET":
            leader = raft.get_leader_id()
            resp_body = json.dumps({"leader": leader}).encode()
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                + resp_body
            )
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        if path == "/kill-leader" and method == "POST":
            # Reject if election is ongoing
            if raft.is_election_ongoing():
                resp_body = json.dumps({
                    "status": "error",
                    "error": "Election ongoing or no leader known. Try again later."
                }).encode()
                resp = (
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                    + resp_body
                )
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            if raft.is_leader():
                # Send response then die
                resp_body = json.dumps({
                    "status": "ok",
                    "message": f"Leader {raft.node_id} is being killed. New election will start."
                }).encode()
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                    + resp_body
                )
                writer.write(resp)
                await writer.drain()
                writer.close()
                
                logger.info("Leader %s received /kill-leader - terminating with exit code 1", raft.node_id)
                # Use os._exit to terminate immediately without cleanup
                os._exit(1)
                return

            # Proxy request to leader
            leader_id = raft.get_leader_id()
            if DCHAT_PUBLIC_HOST:
                # AWS mode: proxy through ALB
                proxy_url = f"{DCHAT_PUBLIC_SCHEME}://{DCHAT_PUBLIC_HOST}/kill-leader"
            else:
                # Local dev mode: proxy directly to leader's HTTP port
                leader_http_port = LOCAL_LEADER_HTTP_PORTS.get(leader_id)
                if leader_http_port is None:
                    resp_body = json.dumps({
                        "status": "error",
                        "error": f"Unknown leader port for {leader_id}"
                    }).encode()
                    resp = (
                        b"HTTP/1.1 500 Internal Server Error\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                        + resp_body
                    )
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return
                proxy_url = f"http://127.0.0.1:{leader_http_port}/kill-leader"

            logger.info("Proxying /kill-leader to leader at %s", proxy_url)
            try:
                proxy_resp = requests.post(proxy_url, timeout=5.0)
                resp_body = proxy_resp.content
                status_line = f"HTTP/1.1 {proxy_resp.status_code} {proxy_resp.reason}\r\n".encode()
                resp = (
                    status_line
                    + b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                    + resp_body
                )
            except requests.exceptions.RequestException as e:
                resp_body = json.dumps({
                    "status": "ok",
                    "message": f"Leader kill request sent. Connection lost (leader likely terminated): {e}"
                }).encode()
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                    + resp_body
                )
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        # --- chat endpoint with RAFT redirects ---

        if path == "/chat" and method == "POST":
            try:
                obj = json.loads(body.decode() or "{}")
            except Exception:
                obj = {}

            # Validate message length for chat messages
            if obj.get("type") == "chat":
                text = obj.get("text", "")

                if len(text) > MAX_MESSAGE_LENGTH:
                    error_body = json.dumps({
                        "status": "error",
                        "error": f"Message too long ({len(text)} chars). Maximum is {MAX_MESSAGE_LENGTH} characters."
                    }).encode()
                    resp = (
                        b"HTTP/1.1 400 Bad Request\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(error_body)}\r\n\r\n".encode()
                        + error_body
                    )
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

            res = await raft.handle_client_command(obj)
            status = res.get("status")

            # 1) Happy path: this node is the leader and the command was accepted
            if status == "ok":
                resp_body = json.dumps(res).encode()
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(resp_body)}\r\n\r\n".encode()
                    + resp_body
                )
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            # 2) Not leader: send 302. Always use the incoming Host header
            if status == "not_leader":
                leader_known = res.get("leader")

                headers = ["HTTP/1.1 302 Found"]

                if leader_known is not None:
                    # --- leader exists ---

                    if DCHAT_PUBLIC_HOST:
                        # AWS / ALB mode: always redirect to the ALB hostname
                        # This satisfies: "Location header always pointing to ALB hostname"
                        redirect_url = f"{DCHAT_PUBLIC_SCHEME}://{DCHAT_PUBLIC_HOST}{path}"
                    else:
                        # Local dev mode: redirect directly to the leader's HTTP port
                        leader_http_port = LOCAL_LEADER_HTTP_PORTS.get(leader_known)
                        if leader_http_port is None:
                            # Fallback: same host/port we were called on (last resort)
                            redirect_url = f"{scheme}://{host_header}{path}"
                        else:
                            redirect_url = f"http://127.0.0.1:{leader_http_port}{path}"

                    headers.append(f"Location: {redirect_url}")
                    logger.info(
                        "Redirecting client to leader=%s via %s",
                        leader_known,
                        redirect_url,
                    )
                else:
                    # During election: no known leader yet. 302 *without* Location.
                    logger.info("No leader known yet; sending 302 with no Location")

                headers.append("Content-Length: 0")

                resp = ("\r\n".join(headers) + "\r\n\r\n").encode()
                writer.write(resp)
                await writer.drain()
                writer.close()
                return


        # default 404
        resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
        writer.write(resp)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle_client, "0.0.0.0", port)
    logger.info("HTTP listening on 0.0.0.0:%d", port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--http-port", type=int, default=9000)
    parser.add_argument("--raft-port", type=int, default=10000)
    parser.add_argument("--peers", default="")
    args = parser.parse_args()

    # Static peers from CLI (for local/dev)
    static_peers = [p for p in args.peers.split(",") if p.strip()]

    # Build peer provider (static vs AWS EC2)
    peer_provider = build_peer_provider_from_env(static_peers)
    resolved_peers = peer_provider.peers()
    logger.info("Resolved RAFT peers for %s: %s", args.id, resolved_peers)  
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Pass peer_provider to enable dynamic peer refresh (removes terminated instances)
    loop.create_task(run_node(args.id, args.http_port, args.raft_port, resolved_peers, peer_provider))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass