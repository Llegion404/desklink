"""Launcher window: saved hosts + PIN -> opens a ViewerWindow session.

The launcher is the app's home screen (`desklink view` with no --host).
Double-click a host (or select + Connect) starts the session; closing the
session returns here with the disconnect reason. Hosts persist in
~/.config/desklink/hosts.json (see hosts.py).

Keyboard: Enter/double-click = connect, Ctrl+N = new host, Delete = remove,
Escape = close the editor.
"""

from __future__ import annotations

import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from .hosts import HostStore, parse_address


class HostRow(Gtk.ListBoxRow):
    def __init__(self, entry: dict):
        super().__init__(activatable=True, selectable=True)
        self.entry = entry
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=8, margin_bottom=8,
                      margin_start=10, margin_end=10)
        title = entry.get("name") or entry["host"]
        name = Gtk.Label(label=title, halign=Gtk.Align.START, xalign=0)
        name.add_css_class("host-name")
        addr = Gtk.Label(label=entry["address"], halign=Gtk.Align.START,
                         xalign=0)
        addr.add_css_class("host-addr")
        addr.add_css_class("dim-label")
        pin = Gtk.Label(label="PIN saved", halign=Gtk.Align.START, xalign=0)
        pin.add_css_class("host-addr")
        pin.add_css_class("dim-label")
        pin.set_visible(bool(entry.get("pin")))
        box.append(name)
        box.append(addr)
        box.append(pin)
        self.set_child(box)


class LauncherWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.app = app
        self.store = HostStore()
        self.store.load()

        self._editing: dict | None = None
        self._confirm_delete_until = 0.0   # two-step delete state
        self._disarm_id = 0

        self._build_ui()
        self._refresh()
        if not self.store.all():
            self._open_editor(None)        # empty state: show the form

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.set_title("DeskLink")
        self.set_default_size(520, 560)

        provider = Gtk.CssProvider()
        css = (".host-name { font-weight: 700; font-size: 105%; } "
               ".host-addr { font-size: 90%; } "
               ".error { color: #e01b24; } ")
        try:
            provider.load_from_string(css)          # GTK >= 4.12
        except AttributeError:
            provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Gtk.HeaderBar()
        add_btn = Gtk.Button()
        add_btn.set_icon_name("list-add-symbolic")
        add_btn.set_tooltip_text("Add host (Ctrl+N)")
        add_btn.connect("clicked", lambda *_: self._open_editor(None))
        header.pack_start(add_btn)
        self.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       margin_top=10, margin_bottom=10,
                       margin_start=10, margin_end=10)

        # -- add/edit form, hidden until needed
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._f_name = Gtk.Entry(placeholder_text="Name (optional)")
        self._f_addr = Gtk.Entry(
            placeholder_text="Address — 192.168.1.20 or 192.168.1.20:9090")
        form.append(self._f_name)
        form.append(self._f_addr)
        fbtns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fsave = Gtk.Button(label="Save host")
        fsave.add_css_class("suggested-action")
        fsave.connect("clicked", self._on_editor_save)
        fcancel = Gtk.Button(label="Cancel")
        fcancel.connect("clicked", lambda *_: self._close_editor())
        fbtns.append(fsave)
        fbtns.append(fcancel)
        form.append(fbtns)
        self._editor = Gtk.Revealer(
            child=form, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            reveal_child=False)
        root.append(self._editor)

        # -- host list
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated",
                             lambda _lb, row: self._connect_row(row))
        self.listbox.connect("selected-rows-changed", self._on_select_changed)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=self.listbox)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(Gtk.Frame(child=scroll))

        # -- connect bar: PIN + save + connect
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._pin = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True,
                                      placeholder_text="PIN")
        self._pin.connect("activate", lambda *_: self._on_connect())
        self._save_pin = Gtk.CheckButton(
            label="Save PIN",
            tooltip_text="Store the PIN in ~/.config/desklink/hosts.json "
                         "(plaintext, mode 0600)")
        self._connect_btn = Gtk.Button(label="Connect")
        self._connect_btn.add_css_class("suggested-action")
        self._connect_btn.connect("clicked", self._on_connect)
        bar.append(self._pin)
        bar.append(self._save_pin)
        bar.append(self._connect_btn)
        root.append(bar)

        # -- host actions + status
        acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._edit_btn = Gtk.Button(label="Rename")
        self._edit_btn.set_sensitive(False)
        self._edit_btn.connect("clicked", lambda *_: self._edit_selected())
        self._del_btn = Gtk.Button(label="Delete")
        self._del_btn.add_css_class("destructive-action")
        self._del_btn.set_sensitive(False)
        self._del_btn.connect("clicked", lambda *_: self._delete_selected())
        spacer = Gtk.Box(hexpand=True)
        acts.append(self._edit_btn)
        acts.append(self._del_btn)
        acts.append(spacer)
        root.append(acts)

        self._status = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0)
        self._status.add_css_class("dim-label")
        self._status.set_ellipsize(3)          # Pango.EllipsizeMode.END
        root.append(self._status)

        self.set_child(root)

    # ---------------------------------------------------------------- list
    def _refresh(self):
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)
        entries = self.store.all()
        if entries:
            for e in entries:
                self.listbox.append(HostRow(e))
        else:
            empty = Gtk.ListBoxRow(selectable=False, activatable=False)
            lbl = Gtk.Label(label="No saved hosts yet — press + to add one")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(24)
            lbl.set_margin_bottom(24)
            empty.set_child(lbl)
            self.listbox.append(empty)
        self._sync_buttons()

    def _selected_row(self) -> HostRow | None:
        row = self.listbox.get_selected_row()
        return row if isinstance(row, HostRow) else None

    def _sync_buttons(self):
        has = self._selected_row() is not None
        self._edit_btn.set_sensitive(has)
        self._del_btn.set_sensitive(has)

    def _on_select_changed(self, _lb):
        self._sync_buttons()
        row = self._selected_row()
        if row:
            pin = row.entry.get("pin") or ""
            self._pin.set_text(pin)
            self._save_pin.set_active(bool(pin))

    # --------------------------------------------------------------- editor
    def _open_editor(self, entry: dict | None):
        self._editing = entry
        self._f_name.set_text(entry["name"] if entry else "")
        self._f_addr.set_text(entry["address"] if entry else "")
        # the address is the identity — editable only while adding
        self._f_addr.set_sensitive(entry is None)
        self._editor.set_reveal_child(True)
        (self._f_addr if entry is None else self._f_name).grab_focus()

    def _close_editor(self):
        self._editor.set_reveal_child(False)
        self._editing = None

    def _on_editor_save(self, *_):
        address = self._f_addr.get_text()
        if parse_address(address) is None:
            self._set_status("Enter a valid address, e.g. 192.168.1.20 "
                             "or 192.168.1.20:9090", error=True)
            self._f_addr.grab_focus()
            return
        name = self._f_name.get_text()
        if self._editing is None:
            host, port = self.store.upsert(address, name=name)
            self._set_status(f"Saved {name or f'{host}:{port}'}")
        else:
            old = self._editing
            self.store.upsert(old["address"], name=name)
            self._set_status(f"Renamed to {name or old['address']}")
        self.store.save()
        self._close_editor()
        self._refresh()

    # -------------------------------------------------------------- actions
    def _connect_row(self, row):
        if isinstance(row, HostRow):
            self.listbox.select_row(row)
            self._on_connect()

    def _on_connect(self, *_):
        row = self._selected_row()
        if row is None:
            self._set_status("Select a host first (or add one with +)",
                             error=True)
            return
        entry = row.entry
        pin = self._pin.get_text()
        if not pin:
            self._set_status(f"Enter the PIN for {entry['address']}",
                             error=True)
            self._pin.grab_focus()
            return
        host, port = self.store.upsert(entry["address"], pin=pin,
                                       save_pin=self._save_pin.get_active())
        self.store.save()
        self._refresh()                     # "PIN saved" tag on the row
        self._set_status(f"Connecting to {host}:{port}…")
        self._connect_btn.set_sensitive(False)
        self.app.open_session(host, port, pin)

    def _edit_selected(self):
        row = self._selected_row()
        if row:
            self._open_editor(row.entry)

    def _delete_selected(self):
        row = self._selected_row()
        if row is None:
            return
        now = time.monotonic()
        if now > self._confirm_delete_until:
            # two-step delete: first click arms the button for 4 s
            self._confirm_delete_until = now + 4.0
            self._del_btn.set_label("Really delete?")
            if self._disarm_id:
                GLib.source_remove(self._disarm_id)
            self._disarm_id = GLib.timeout_add(4000, self._disarm_delete)
            return
        entry = row.entry
        self.store.remove(entry["host"], entry["port"])
        self.store.save()
        self._disarm_delete()
        self._refresh()
        self._set_status(f"Removed {entry['address']}")

    def _disarm_delete(self):
        self._confirm_delete_until = 0.0
        self._del_btn.set_label("Delete")
        self._disarm_id = 0
        return False                        # remove the timeout source

    def _set_status(self, text: str, error: bool = False):
        self._status.set_text(text)
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")

    # -------------------------------------------------------------- events
    def _on_key(self, _ctl, keyval, _kc, state):
        if keyval == Gdk.KEY_Escape and self._editor.get_reveal_child():
            self._close_editor()
            return True
        if (state & Gdk.ModifierType.CONTROL_MASK) and \
                keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self._open_editor(None)
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            # don't hijack Delete while typing in a text field
            focus = self.get_focus()
            if isinstance(focus, Gtk.Editable):
                return False
            self._delete_selected()
            return True
        return False

    def _on_close(self, *_):
        self.app.quit()
        return False                        # let the window close for real

    # called by the app when a session window closes
    def session_returned(self, reason: str | None):
        self._connect_btn.set_sensitive(True)
        if reason:
            self._set_status(reason, error="denied" in reason.lower())
        else:
            self._set_status("Disconnected")
        self._pin.grab_focus()
