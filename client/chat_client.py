import threading
import time
import uuid
import requests
import tkinter as tk
from tkinter import simpledialog

from gui import ChatUI
from client import post_with_raft_redirects, get_with_raft_redirects

CLUSTER_URL = "http://127.0.0.1:9000" # Local testing
# CLUSTER_URL = "http://DChatALB-596522607.eu-north-1.elb.amazonaws.com"

MAX_ROOMS = 5
MAX_MESSAGE_LENGTH = 256

# Debug commands
DEBUG_COMMANDS = {"/instances", "/leader", "/kill-leader", "/clear"}


class ChatApp:
    def __init__(self, username=None):
        # If no username is given, pop up a small dialog first
        if username is None:
            root = tk.Tk()
            root.withdraw()
            username = simpledialog.askstring("Username", "Choose a name:")
            root.destroy()
            if not username:
                username = "anon"

        self.username = username

        # RAFT-backed state from server
        self._all_messages = []      # full log from /messages
        self._current_room = "general"
        self._rooms = {"general"}
        self._pending_ids = set()
        self._polling = True

        # Track active users and when we last saw them
        self._users = set()
        self._user_last_seen = {}

        # Track which committed chat messages we've already rendered
        self._seen_msg_ids = set()
        
        # Track which messages we've already processed for user presence
        # (separate from _seen_msg_ids to avoid bugs with historical messages)
        self._processed_for_presence = set()


        self.ui = ChatUI(
            username=username,
            send_callback=self._on_send_text,
            on_close=self._on_close,
            room_change_callback=self._on_room_change,
            room_add_callback=self._on_room_add_requested,
            room_delete_callback=self._on_room_delete_requested,
        )

        self._current_room = "general"
        self.ui.set_rooms(sorted(self._rooms), select=self._current_room)

        # Show ourselves immediately in the Users list
        now = time.time()
        self._users.add(self.username)
        self._user_last_seen[self.username] = now
        self.ui.add_user_connected(self.username)

        # Start as "disconnected" until health check
        self.ui.set_status(False)
        self.ui.add_system_message(f"Starting client for user '{username}'")
        self.ui.add_system_message(f"Using cluster URL: {CLUSTER_URL}")
        self.ui.add_system_message(f"Current room: {self._current_room}")

        # Initial health check
        t = threading.Thread(target=self._initial_health_check, daemon=True)
        t.start()

        # Send user_connected event to the server to notify other clients
        conn_thread = threading.Thread(target=self._send_user_connected, daemon=True)
        conn_thread.start()

        # Background polling of /messages
        poller = threading.Thread(target=self._poll_messages_loop, daemon=True)
        poller.start()
        
        # Background heartbeat to keep user presence alive
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat.start()

    # ---------- background health check ----------

    def _send_user_connected(self) -> None:
        """Send a user_connected event to the server so other clients see the user immediately."""
        cmd = {"type": "user_connected", "user": self.username, "id": str(uuid.uuid4())}
        try:
            post_with_raft_redirects(CLUSTER_URL, cmd)
        except Exception as e:
            pass
    
    def _heartbeat_loop(self) -> None:
        """Periodically send heartbeat to keep user presence alive for other clients."""
        while self._polling:
            # Send heartbeat BEFORE sleeping (fixes initial 30s gap)
            # Include unique ID so each heartbeat is tracked separately
            cmd = {"type": "user_heartbeat", "user": self.username, "id": str(uuid.uuid4())}
            try:
                post_with_raft_redirects(CLUSTER_URL, cmd, timeout=2.0)
            except Exception:
                pass  # Don't spam errors for heartbeat failures
            
            time.sleep(20)  # Send heartbeat every 20 seconds (more frequent than before)
            if not self._polling:
                break

    def _initial_health_check(self) -> None:
        try:
            r = requests.get(CLUSTER_URL + "/health", timeout=2)
            if r.status_code == 200:
                self.ui.add_system_message("Health check OK, cluster reachable.")
                self.ui.set_status(True)
            else:
                self.ui.add_system_message(f"Health check returned {r.status_code}.")
                self.ui.set_status(False)
        except Exception as e:
            self.ui.add_system_message(f"Health check failed: {e}")
            self.ui.set_status(False)

    # ---------- room handling ----------

    def _on_room_change(self, new_room: str):
        if not new_room:
            new_room = "general"
        self._current_room = new_room
        self.ui.add_system_message(f"Switched to room '{new_room}'")

        # Clear chat and redraw messages for this room from our local log
        self.ui.clear_messages()
        for m in self._all_messages:
            msg_type = m.get("type", "chat")
            if msg_type != "chat":
                continue
            room = m.get("room", "general")
            if room == self._current_room:
                user = m.get("user", "?")
                text = m.get("text", "")
                timestamp = m.get("timestamp")
                self.ui.add_message(user, text, timestamp, style="normal")

    def _on_room_add_requested(self, room_name: str):
        if len(self._rooms) >= MAX_ROOMS and room_name not in self._rooms:
            self.ui.add_system_message(f"Cannot add room '{room_name}': max {MAX_ROOMS} rooms.")
            return
        if room_name in self._rooms:
            self.ui.add_system_message(f"Room '{room_name}' already exists.")
            return

        cmd = {"type": "room_add", "room": room_name, "user": self.username}
        self.ui.add_system_message(f"Requesting creation of room '{room_name}'...")
        t = threading.Thread(target=self._send_room_command, args=(cmd, "created"), daemon=True)
        t.start()

    def _on_room_delete_requested(self, room_name: str):
        if room_name == "general":
            self.ui.add_system_message("Cannot delete the 'general' room.")
            return
        if room_name not in self._rooms:
            self.ui.add_system_message(f"Room '{room_name}' does not exist.")
            return

        cmd = {"type": "room_delete", "room": room_name, "user": self.username}
        self.ui.add_system_message(f"Requesting deletion of room '{room_name}'...")
        t = threading.Thread(target=self._send_room_command, args=(cmd, "deleted"), daemon=True)
        t.start()

    def _send_room_command(self, cmd: dict, action_word: str):
        try:
            resp = post_with_raft_redirects(CLUSTER_URL, cmd)
            data = resp.json()
            if data.get("status") == "ok":
                self.ui.add_system_message(f"Room '{cmd.get('room')}' {action_word} (committed).")
                self.ui.set_status(True)
            else:
                self.ui.add_system_message(f"Room command failed: {data}")
                self.ui.set_status(False)
        except Exception as e:
            self.ui.add_system_message(f"Room command error: {e}")
            self.ui.set_status(False)

    # ---------- send handling ----------

    def _on_send_text(self, text: str) -> None:
        """Called from ChatUI (GUI thread) when the user hits Enter or Send."""
        stripped = text.strip()
        
        # Check for debug commands
        if stripped in DEBUG_COMMANDS:
            self._handle_debug_command(stripped)
            return

        # Validate message length (UTF-8 characters)
        if len(text) > MAX_MESSAGE_LENGTH:
            self.ui.add_system_message(
                f"Message too long ({len(text)} chars). Maximum is {MAX_MESSAGE_LENGTH} characters."
            )
            return

        room = self._current_room or "general"
        msg_id = str(uuid.uuid4())

        # Refresh our own last seen timestamp (we're clearly active!)
        self._user_last_seen[self.username] = time.time()

        # Local gray echo
        self._pending_ids.add(msg_id)
        self.ui.add_pending_message(msg_id, self.username, text)

        payload = {
            "type": "chat",
            "user": self.username,
            "text": text,
            "room": room,
            "id": msg_id,
        }

        t = threading.Thread(target=self._send_message_background, args=(payload,))
        t.daemon = True
        t.start()

    def _handle_debug_command(self, command: str) -> None:
        """Handle debug commands in a background thread."""
        self.ui.add_system_message(f"Executing debug command: {command}")
        t = threading.Thread(target=self._execute_debug_command, args=(command,), daemon=True)
        t.start()

    def _execute_debug_command(self, command: str) -> None:
        """Execute a debug command and display the result."""
        try:
            if command == "/instances":
                resp = get_with_raft_redirects(CLUSTER_URL, "/instances", timeout=5.0)
                data = resp.json()
                self.ui.add_system_message(f"[DEBUG] Instances: {data}")
                
            elif command == "/leader":
                resp = get_with_raft_redirects(CLUSTER_URL, "/leader", timeout=5.0)
                data = resp.json()
                self.ui.add_system_message(f"[DEBUG] Leader: {data}")
                
            elif command == "/kill-leader":
                # Use requests directly since we need POST and special error handling
                try:
                    resp = requests.post(CLUSTER_URL + "/kill-leader", timeout=10.0)
                    data = resp.json()
                    if resp.status_code == 200:
                        self.ui.add_system_message(f"[DEBUG] Kill leader: {data}")
                    else:
                        self.ui.add_system_message(f"[DEBUG] Kill leader failed: {data}")
                except requests.exceptions.RequestException as e:
                    self.ui.add_system_message(f"[DEBUG] Kill leader: Request sent (connection lost - leader likely killed)")

            elif command == "/clear":
                room = self._current_room or "general"
                cmd = {
                    "type": "room_clear",
                    "room": room,
                    "user": self.username,
                    "id": str(uuid.uuid4()),
                }
                self.ui.add_system_message(f"Clearing all messages in room '{room}'...")
                resp = post_with_raft_redirects(CLUSTER_URL, cmd)
                data = resp.json()
                if data.get("status") == "ok":
                    self.ui.add_system_message(f"Room '{room}' cleared successfully.")
                else:
                    self.ui.add_system_message(f"Failed to clear room: {data}")
                    
            self.ui.set_status(True)
            
        except Exception as e:
            self.ui.add_system_message(f"[DEBUG] Command failed: {e}")
            self.ui.set_status(False)

    def _send_message_background(self, payload: dict) -> None:
        try:
            resp = post_with_raft_redirects(CLUSTER_URL, payload)

            data = resp.json()
            status = data.get("status")

            if status == "ok":
                self.ui.set_status(True)
            else:
                self.ui.add_system_message(f"Server responded with: {data}")
                self.ui.set_status(False)

        except Exception as e:
            # Covers redirect loops, long elections, network errors, etc.
            self.ui.add_system_message(f"Error sending message: {e}")
            self.ui.set_status(False)

    # ---------- background poll of /messages ----------

    def _poll_messages_loop(self) -> None:
        """
        Periodically fetch /messages from the cluster (with redirect handling)
        and:
          - update the room list based on room_add/room_delete events
          - show chat messages for the current room once per unique id
          - clear gray pending lines when their committed message arrives
          - update active users list and prune inactive ones
        """
        while self._polling:
            try:
                resp = get_with_raft_redirects(CLUSTER_URL, "/messages", timeout=2.0)
                msgs = resp.json()  # expected to be a list of dicts
                if isinstance(msgs, list):
                    # keep a copy of the full log for room switching
                    self._all_messages = msgs

                    for m in msgs:
                        msg_type = m.get("type", "chat")
                        timestamp = m.get("timestamp") 
                        user = m.get("user")
                        msg_id = m.get("id")
                        
                        # Create a unique key for this message for presence tracking
                        # Use msg_id if available, otherwise create a pseudo-id from content
                        if msg_id:
                            presence_key = msg_id
                        else:
                            # For events without id (user_connected, room_add, etc.)
                            # create a pseudo-key from type + user + room
                            presence_key = f"{msg_type}:{user}:{m.get('room', '')}"

                        # Skip already-processed messages entirely
                        if presence_key in self._processed_for_presence:
                            continue
                        
                        self._processed_for_presence.add(presence_key)

                        # --- Active users tracking for chat/heartbeat messages ---
                        if user and msg_type in ("chat", "user_heartbeat"):
                            now = time.time()
                            if user not in self._users:
                                self._users.add(user)
                                self.ui.add_user_connected(user)
                            self._user_last_seen[user] = now

                        # Handle user_connected events to add users to the list
                        if msg_type == "user_connected":
                            if user and user not in self._users:
                                self._users.add(user)
                                self.ui.add_user_connected(user)
                            if user:
                                self._user_last_seen[user] = time.time()
                        
                        # Handle user_disconnected events to remove users from the list
                        elif msg_type == "user_disconnected":
                            if user and user in self._users:
                                self._users.remove(user)
                                self.ui.remove_user_connected(user)
                                if user in self._user_last_seen:
                                    del self._user_last_seen[user]

                        if msg_type == "room_add":
                            room = m.get("room")
                            if room and room not in self._rooms:
                                self._rooms.add(room)
                                self.ui.set_rooms(sorted(self._rooms), select=self._current_room)

                        elif msg_type == "room_delete":
                            room = m.get("room")
                            if room and room in self._rooms and room != "general":
                                self._rooms.remove(room)
                                if self._current_room == room:
                                    self._current_room = "general"
                                    self.ui.set_rooms(sorted(self._rooms), select=self._current_room)
                                    self._on_room_change(self._current_room)
                                else:
                                    self.ui.set_rooms(sorted(self._rooms), select=self._current_room)

                        elif msg_type == "room_clear":
                            room = m.get("room", "general")
                            # Remove cleared messages from local seen IDs so they don't block future messages
                            cleared_ids = {
                                msg.get("id") for msg in self._all_messages
                                if msg.get("type") == "chat" and msg.get("room", "general") == room and msg.get("id")
                            }
                            self._seen_msg_ids -= cleared_ids
                            if room == self._current_room:
                                self.ui.clear_messages()
                                user_who_cleared = m.get("user", "Someone")
                                self.ui.add_system_message(f"Room '{room}' was cleared by {user_who_cleared}.")

                        elif msg_type == "chat":
                            # msg_id already retrieved above
                            timestamp = m.get("timestamp")  # or "ts"
                            # If this committed message corresponds to a local pending echo,
                            # remove the gray line.
                            if msg_id and msg_id in self._pending_ids:
                                self._pending_ids.remove(msg_id)
                                self.ui.remove_pending_message(msg_id)

                            # De-duplication: only render each id once
                            if msg_id and msg_id in self._seen_msg_ids:
                                continue  # already shown

                            if msg_id:
                                self._seen_msg_ids.add(msg_id)

                            room = m.get("room", "general")
                            if room == self._current_room:
                                user_display = m.get("user", "?")
                                text = m.get("text", "")
                                self.ui.add_message(user_display, text, timestamp, style="normal")

                    # Prune inactive users (no activity in last 90 seconds)
                    # Never prune ourselves - we know we're connected!
                    now = time.time()
                    inactive = [
                        u for u, last in self._user_last_seen.items()
                        if now - last > 90 and u != self.username
                    ]
                    for u in inactive:
                        if u in self._users:
                            self._users.remove(u)
                            self.ui.remove_user_connected(u)
                        del self._user_last_seen[u]

                    if msgs:
                        self.ui.set_status(True)

            except Exception as e:
                self.ui.add_system_message(f"Error fetching messages: {e}")
                self.ui.set_status(False)

            time.sleep(1.0)



    # ---------- close ----------

    def _on_close(self) -> None:
        self._polling = False
        # Notify server of disconnect
        self._send_user_disconnected()
    
    def _send_user_disconnected(self) -> None:
        """Send a user_disconnected event to the server so other clients remove the user."""
        cmd = {"type": "user_disconnected", "user": self.username, "id": str(uuid.uuid4())}
        try:
            post_with_raft_redirects(CLUSTER_URL, cmd, timeout=1.0)
        except Exception:
            pass

    def run(self) -> None:
        self.ui.run()


if __name__ == "__main__":
    app = ChatApp(username=None)  # username will be asked via dialog
    app.run()