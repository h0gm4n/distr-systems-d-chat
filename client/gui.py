import tkinter as tk
from tkinter import scrolledtext, simpledialog
from datetime import datetime
import random


class ChatUI:
    COLOR_PALETTE = ['yellow4', 'red', 'forest green', 'saddle brown', 'purple1', 
                    'dark orange', 'royal blue', 'hot pink', 'MistyRose4', 'violet red']
    def __init__(
        self,
        username='anon',
        send_callback=None,
        on_close=None,
        room_change_callback=None,
        room_add_callback=None,
        room_delete_callback=None,
    ):
        self.username = username
        self.send_callback = send_callback
        self.on_close = on_close
        self.room_change_callback = room_change_callback
        self.room_add_callback = room_add_callback
        self.room_delete_callback = room_delete_callback

        self.user_colors = {}
        self.used_colors = set()

        self.root = tk.Tk()
        self.root.title(f'D-Chat: {self.username}')
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self._build()

    def _build(self):
        # layout: left sidebar (rooms + users) + main chat area on the right
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Sidebar frame
        sidebar = tk.Frame(self.root)
        sidebar.grid(row=0, column=0, rowspan=2, sticky='ns', padx=(5, 0), pady=5)

        # ---- Rooms section ----
        rooms_label = tk.Label(sidebar, text='Rooms')
        rooms_label.pack(anchor='nw')

        room_box_frame = tk.Frame(sidebar)
        room_box_frame.pack(anchor='nw', fill='x')

        self.room_list = tk.Listbox(room_box_frame, height=5, exportselection=False)
        self.room_list.pack(side='left', fill='y')
        room_scroll = tk.Scrollbar(room_box_frame, orient='vertical', command=self.room_list.yview)
        room_scroll.pack(side='right', fill='y')
        self.room_list.config(yscrollcommand=room_scroll.set)
        self.room_list.bind('<<ListboxSelect>>', self._on_room_selected)

        # Add / delete room buttons
        room_btn_frame = tk.Frame(sidebar)
        room_btn_frame.pack(anchor='nw', pady=(2, 8))
        tk.Button(room_btn_frame, text='+', width=3, command=self._on_add_room).pack(side='left')
        tk.Button(room_btn_frame, text='-', width=3, command=self._on_delete_room).pack(side='left', padx=(2, 0))

        # ---- Users section ----
        lbl = tk.Label(sidebar, text='Users')
        lbl.pack(anchor='nw')
       
        self.user_list = tk.Listbox(sidebar, height=10, exportselection=False)
        self.user_list.pack(side='left', fill='y', expand=False)
        ul_scroll = tk.Scrollbar(sidebar, orient='vertical', command=self.user_list.yview)
        ul_scroll.pack(side='right', fill='y')
        self.user_list.config(yscrollcommand=ul_scroll.set)

        # Main chat area frame
        main = tk.Frame(self.root)
        main.grid(row=0, column=1, padx=5, pady=5, sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        # Chat text area
        self.txt = scrolledtext.ScrolledText(main, wrap=tk.WORD, state='disabled', width=60, height=20)
        self.txt.grid(row=0, column=0, columnspan=2, sticky='nsew')

        # Text styles (base styles)
        self.txt.tag_configure('normal', foreground='black')
        self.txt.tag_configure('local_echo', foreground='gray')
        self.txt.tag_configure('system', foreground='blue')

        # Input line
        self.entry = tk.Entry(main, width=50)
        self.entry.grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.entry.bind('<Return>', self._on_send)

        self.send_btn = tk.Button(main, text='Send', command=self._on_send)
        self.send_btn.grid(row=1, column=1, padx=5, pady=5, sticky='e')

        # Status line
        self.status_label = tk.Label(main, text='Status: Disconnected', fg='red')
        self.status_label.grid(row=2, column=0, columnspan=2, sticky='w', padx=5, pady=(0, 5))

    def _get_user_color(self, username):
        """
        Assigns or retrieves a color and Tkinter tag for a username.
        """
        if username not in self.user_colors:
            available_colors = [c for c in self.COLOR_PALETTE if c not in self.used_colors]
            if not available_colors:
                color = random.choice(self.COLOR_PALETTE)
            else:
                color = random.choice(available_colors)

            tagname = f"user_{username.replace(' ', '_')}_tag"
            self.user_colors[username] = {'color': color, 'tag': tagname}
            self.used_colors.add(color)

            # Configure the tag in the ScrolledText widget
            self.txt.tag_configure(tagname, foreground=color)

        return self.user_colors[username]['color'], self.user_colors[username]['tag']

    # ---------- rooms ----------

    def set_rooms(self, rooms, select=None):
        """
        Replace the room list with `rooms` (list of names), and select `select`
        or the first room if select is not provided.
        """
        self.room_list.delete(0, tk.END)
        for r in rooms:
            self.room_list.insert(tk.END, r)

        if not rooms:
            return

        if select and select in rooms:
            idx = rooms.index(select)
        else:
            idx = 0

        self.room_list.selection_clear(0, tk.END)
        self.room_list.selection_set(idx)

    def get_current_room(self) -> str:
        sel = self.room_list.curselection()
        if not sel:
            return ''
        return self.room_list.get(sel[0])

    def _on_room_selected(self, event=None):
        room = self.get_current_room()
        if self.room_change_callback and room:
            self.room_change_callback(room)

    def _on_add_room(self):
        # UI just asks for a name and delegates to callback; actual creation
        # is driven by RAFT events so all clients stay in sync.
        name = simpledialog.askstring("New room", "Room name:", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if self.room_add_callback:
            self.room_add_callback(name)
        else:
            self.add_system_message("Room creation not supported in this client.")

    def _on_delete_room(self):
        room = self.get_current_room()
        if not room:
            return
        if self.room_delete_callback:
            self.room_delete_callback(room)
        else:
            self.add_system_message("Room deletion not supported in this client.")

    # ---------- helpers for messages ----------

    def add_message(self, user, message, timestamp=None, style='normal'):
        """Public API: add a chat line. Safe to call from other threads."""
        if timestamp is None:
            ts = datetime.now().strftime('%H:%M')
        else:
            ts = timestamp
        try:
            self.root.after(0, lambda: self._add_message_ui(user, message, ts, style))
        except Exception:
            self._add_message_ui(user, message, ts, style)

    def _add_message_ui(self, user, message, timestamp=None, style='normal'):
        # Get color/tag for the user
        _, user_tag = self._get_user_color(user)

        line_prefix = f"{timestamp}  "
        user_part = f"{user}: "
        message_part = f"{message}\n"

        self.txt.configure(state='normal')
        
        # Insert timestamp and space (normal style)
        self.txt.insert(tk.END, line_prefix, ('normal',))
        
        # Insert username and colon (with user-specific color tag)
        self.txt.insert(tk.END, user_part, (user_tag, style))
        
        # Insert message content (normal style)
        self.txt.insert(tk.END, message_part, ('normal', style))
        
        self.txt.see(tk.END)
        self.txt.configure(state='disabled')

    def add_pending_message(self, msg_id, user, message):
        """
        Add a gray 'pending' line for a message, tagged with msg_id so it can
        be removed when the committed message arrives from the server.
        """
        try:
            self.root.after(0, lambda: self._add_pending_message_ui(msg_id, user, message))
        except Exception:
            self._add_pending_message_ui(msg_id, user, message)

    def _add_pending_message_ui(self, msg_id, user, message):
        ts = datetime.now().strftime('%H:%M')
        line = f"{ts}  {user}: {message}\n"
        tagname = f"pending_{msg_id}"
        self.txt.configure(state='normal')
        self.txt.insert(tk.END, line, ('local_echo', tagname))
        self.txt.see(tk.END)
        self.txt.configure(state='disabled')

    def remove_pending_message(self, msg_id):
        """
        Remove the gray 'pending' line for this msg_id, if present.
        """
        try:
            self.root.after(0, lambda: self._remove_pending_message_ui(msg_id))
        except Exception:
            self._remove_pending_message_ui(msg_id)

    def _remove_pending_message_ui(self, msg_id):
        tagname = f"pending_{msg_id}"
        ranges = self.txt.tag_ranges(tagname)
        if ranges:
            start = ranges[0]
            end = ranges[1] if len(ranges) >= 2 else ranges[0]
            self.txt.configure(state='normal')
            self.txt.delete(start, end)
            self.txt.configure(state='disabled')


    def add_system_message(self, text):
        """Public API: system messages like errors, info."""
        try:
            self.root.after(0, lambda: self._add_system_message_ui(text))
        except Exception:
            self._add_system_message_ui(text)

    def _add_system_message_ui(self, text):
        ts = datetime.now().strftime('%H:%M')
        line = f"{ts}  [system] {text}\n"
        self.txt.configure(state='normal')
        self.txt.insert(tk.END, line, ('system',))
        self.txt.see(tk.END)
        self.txt.configure(state='disabled')

    def clear_messages(self):
        """Clear the chat text area."""
        try:
            self.root.after(0, self._clear_messages_ui)
        except Exception:
            self._clear_messages_ui()

    def _clear_messages_ui(self):
        self.txt.configure(state='normal')
        self.txt.delete('1.0', tk.END)
        self.txt.configure(state='disabled')

    # ---------- user list (sidebar) ----------

    def add_user_connected(self, username):
        """
        Add the username to the sidebar list and a chat line.
        Safe to call from other threads.
        """
        try:
            self.root.after(0, lambda: self._add_user_connected_ui(username))
        except Exception:
            self._add_user_connected_ui(username)

    def _add_user_connected_ui(self, username):
        # Ensure the user has a color assigned and the tag is created
        user_color, _ = self._get_user_color(username)
        
        existing = self.user_list.get(0, tk.END)
        if username not in existing:
            # Insert the username into the listbox
            self.user_list.insert(tk.END, username)
            
            # Change the color of the inserted item
            idx = self.user_list.get(0, tk.END).index(username)
            self.user_list.itemconfig(idx, {'fg': user_color})

        self._add_system_message_ui(f"{username} seen in chat")

    def remove_user_connected(self, username):
        """
        Kept for compatibility, not heavily used in HTTP mode.
        """
        try:
            self.root.after(0, lambda: self._remove_user_connected_ui(username))
        except Exception:
            self._remove_user_connected_ui(username)

    def _remove_user_connected_ui(self, username):
        items = list(self.user_list.get(0, tk.END))
        for i, v in enumerate(items):
            if v == username:
                # Remove from listbox
                self.user_list.delete(i)
                
                # Remove color/tag from internal mapping (optional, but good for cleanup)
                if username in self.user_colors:
                    color_to_remove = self.user_colors[username]['color']
                    # Only remove from used_colors if no other user is using it
                    if all(v['color'] != color_to_remove for u, v in self.user_colors.items() if u != username):
                        self.used_colors.discard(color_to_remove)
                    del self.user_colors[username]
                break
        self._add_system_message_ui(f"{username} disconnected")

    # ---------- status indicator ----------

    def set_status(self, connected: bool):
        """Show 'Connected' / 'Disconnected' with color."""
        try:
            self.root.after(0, lambda: self._set_status_ui(connected))
        except Exception:
            self._set_status_ui(connected)

    def _set_status_ui(self, connected: bool):
        if connected:
            self.status_label.config(text='Status: Connected', fg='green')
        else:
            self.status_label.config(text='Status: Disconnected', fg='red')

    # ---------- send / close ----------

    def _on_send(self, event=None):
        text = self.entry.get().strip()
        if not text:
            return
        if self.send_callback:
            self.send_callback(text)
        self.entry.delete(0, tk.END)

    def _on_close(self):
        try:
            if self.on_close:
                try:
                    self.on_close()
                except Exception:
                    pass
        finally:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ui = ChatUI(username='anon')
    ui.add_system_message("Chat UI only. Run chat_client.py for full client.")
    ui.run()