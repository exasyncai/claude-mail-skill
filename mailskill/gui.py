"""The `mailskill add` window: an address, a password, one button.

Everything that decides something lives in setup.py; this file only draws it. The
window reacts while you type: a Microsoft address hides the password field and offers
the browser sign-in, Gmail and friends get their app-password link. Discovery, login
test and storing run in a worker thread so the window never freezes.
"""
from __future__ import annotations

import queue
import threading
import webbrowser

from . import setup
from .oauth import OAuthError

TITLE = "claude-mail-skill: add a mailbox"
DEBOUNCE_MS = 700
CLOSE_AFTER_MS = 2500


def available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


class AddDialog:
    """Build with a tkinter root; `run()` blocks until the window closes and returns the result dict or None."""

    def __init__(self, root, *, client_id: str | None = None, discover_fn=setup.discover, sign_in=None,
                 mailbox_cls=setup.Mailbox, preset_address: str = ""):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root = root
        self.client_id = client_id
        self.discover_fn = discover_fn
        self.sign_in = sign_in
        self.mailbox_cls = mailbox_cls
        self.plan: setup.Plan | None = None
        self.result: dict | None = None
        self.events: queue.Queue = queue.Queue()
        self._debounce = None
        self._planning_for = ""
        self._busy = False

        root.title(TITLE)
        root.resizable(False, False)
        try:
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
        except Exception:
            pass
        frm = ttk.Frame(root, padding=18)
        frm.grid(sticky="nsew")

        ttk.Label(frm, text="Email address").grid(row=0, column=0, sticky="w")
        self.addr = tk.StringVar(value=preset_address)
        self.addr_entry = ttk.Entry(frm, textvariable=self.addr, width=44)
        self.addr_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        self.addr_entry.bind("<KeyRelease>", self._on_typing)
        self.addr_entry.bind("<FocusOut>", lambda e: self._plan_now())
        self.addr_entry.bind("<Return>", lambda e: self._connect())

        self.pw_label = ttk.Label(frm, text="Password")
        self.pw = tk.StringVar()
        self.pw_entry = ttk.Entry(frm, textvariable=self.pw, width=44, show="•")
        self.pw_entry.bind("<Return>", lambda e: self._connect())
        self.pw_label.grid(row=2, column=0, sticky="w")
        self.pw_entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 6))

        self.hint = ttk.Label(frm, text="", wraplength=360, foreground="#444")
        self.hint.grid(row=4, column=0, columnspan=2, sticky="w")
        self.link_url = ""
        self.link = ttk.Label(frm, text="", foreground="#1B16FF", cursor="hand2")
        self.link.grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.link.bind("<Button-1>", lambda e: self.link_url and webbrowser.open(self.link_url))

        self.button = ttk.Button(frm, text="Connect", command=self._connect)
        self.button.grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.status = ttk.Label(frm, text="", wraplength=360)
        self.status.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        frm.columnconfigure(0, weight=1)

        (self.pw_entry if preset_address else self.addr_entry).focus_set()
        if preset_address:
            self._plan_now()
        root.after(100, self._poll)

    # ------------------------------------------------------------ view

    def _apply_view(self, plan: setup.Plan | None):
        v = plan.view if plan else {"password_field": True, "button": "Connect", "hint": "", "link": ""}
        if v["password_field"]:
            self.pw_label.grid()
            self.pw_entry.grid()
        else:
            self.pw_label.grid_remove()
            self.pw_entry.grid_remove()
        self.button.configure(text=v["button"])
        self.hint.configure(text=v["hint"])
        self.link_url = v["link"]
        self.link.configure(text=v["link"])

    def _say(self, msg: str):
        self.status.configure(text=msg)

    # ------------------------------------------------------------ discovery while typing

    def _on_typing(self, _event=None):
        if self._debounce:
            self.root.after_cancel(self._debounce)
        addr = self.addr.get().strip().lower()
        k = setup.quick_kind(addr)
        if k == "oauth" and (not self.plan or self.plan.address != addr):
            self.plan = setup.Plan(address=addr, kind="oauth", provider="microsoft365",
                                   hint="Microsoft account: the sign-in opens in your browser, no password is typed here.")
            self._apply_view(self.plan)
        elif self.plan and self.plan.address != addr:
            self.plan = None
            self._apply_view(None)
        self._debounce = self.root.after(DEBOUNCE_MS, self._plan_now)

    def _plan_now(self):
        addr = self.addr.get().strip().lower()
        if not setup.valid_address(addr) or self._busy:
            return
        if (self.plan and self.plan.address == addr and self.plan.candidate) or self._planning_for == addr:
            return
        self._planning_for = addr
        if not (self.plan and self.plan.kind == "oauth"):
            self._say("Looking up the mail server ...")

        def work():
            try:
                plan = setup.plan_for(addr, discover_fn=self.discover_fn)
                self.events.put(("plan", addr, plan))
            except setup.SetupError as e:
                self.events.put(("plan_error", addr, str(e)))
            except Exception as e:
                self.events.put(("plan_error", addr, f"lookup failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ connect

    def _connect(self):
        if self._busy:
            return
        addr = self.addr.get().strip().lower()
        if not setup.valid_address(addr):
            self._say("Please enter an email address.")
            return
        plan = self.plan if (self.plan and self.plan.address == addr) else None
        secret = self.pw.get()
        if plan and plan.kind == "password" and not secret:
            self._say("Please enter the password.")
            self.pw_entry.focus_set()
            return
        self._busy = True
        self.button.state(["disabled"])
        self._say("Connecting ...")

        def work():
            try:
                p = plan
                if p is None or not p.candidate:
                    self.events.put(("status", "Looking up the mail server ..."))
                    p = setup.plan_for(addr, discover_fn=self.discover_fn)
                    self.events.put(("plan", addr, p))
                if p.kind == "none":
                    raise setup.SetupError(p.hint, code="none")
                if p.kind == "oauth":
                    acc, folders, where = setup.sign_in_microsoft(
                        p, client_id=self.client_id, status=lambda m: self.events.put(("status", m)),
                        sign_in=self.sign_in, mailbox_cls=self.mailbox_cls)
                else:
                    if not secret:
                        raise setup.SetupError("Please enter the password.", code="password")
                    acc = setup.account_for(p)
                    self.events.put(("status", f"Testing the login at {acc.host} ..."))
                    folders = setup.test_login(acc, secret, mailbox_cls=self.mailbox_cls)
                    where = setup.finish_password(acc, secret)
                self.events.put(("done", {"address": acc.address, "host": acc.host, "auth": acc.auth,
                                          "folders": len(folders), "stored": where}))
            except (setup.SetupError, OAuthError) as e:
                self.events.put(("fail", str(e), getattr(e, "hint", ""), getattr(e, "link", "") or getattr(e, "admin_consent_url", "")))
            except Exception as e:
                self.events.put(("fail", f"{type(e).__name__}: {e}", "", ""))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ worker -> window

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "plan":
            _, addr, plan = ev[1], ev[1], ev[2]
            self._planning_for = ""
            if self.addr.get().strip().lower() != addr:
                return
            self.plan = plan
            self._apply_view(plan)
            if not self._busy:
                if plan.kind == "none":
                    self._say(plan.hint)
                elif plan.kind == "oauth":
                    self._say("")
                else:
                    self._say(f"Server: {plan.candidate.host}:{plan.candidate.port}. Enter the password and press Connect.")
                    self.pw_entry.focus_set()
        elif kind == "plan_error":
            self._planning_for = ""
            if not self._busy:
                self._say(ev[2])
        elif kind == "status":
            self._say(ev[1])
        elif kind == "done":
            self.result = ev[1]
            r = self.result
            how = "signed in with Microsoft" if r["auth"] == "oauth" else "login ok"
            self._say(f"Done: {r['address']}, {how}, {r['folders']} folders. Stored in your keychain. This window closes itself.")
            self.button.configure(text="Close", command=self.root.destroy)
            self.button.state(["!disabled"])
            self.root.after(CLOSE_AFTER_MS, self.root.destroy)
        elif kind == "fail":
            self._busy = False
            self.button.state(["!disabled"])
            msg, hint, link = ev[1], ev[2], ev[3]
            self._say(msg)
            if hint:
                self.hint.configure(text=hint)
            if link:
                self.link_url = link
                self.link.configure(text=link)

    def run(self) -> dict | None:
        self.root.mainloop()
        return self.result


def run_add_dialog(*, client_id: str | None = None, preset_address: str = "") -> dict | None:
    """Open the window; returns the result dict on success, None when closed without one."""
    import tkinter as tk

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except Exception:
        pass
    dlg = AddDialog(root, client_id=client_id, preset_address=preset_address)
    return dlg.run()
