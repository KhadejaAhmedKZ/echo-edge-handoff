import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import queue
import threading
import time

import credentials
from config import load_config, save_config, clear_config
from wifi_controller import (
    JoinRefused,
    choose_target,
    create_wifi_controller,
)

# Reading the associated SSID goes through system_profiler, which takes a few
# seconds, so every read happens off the UI thread.
AUTO_REFRESH_MS = 20000
UI_QUEUE_MS = 100
VERIFY_TIMEOUT_S = 60
VERIFY_INTERVAL_S = 2


class WifiSwitchApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Wi-Fi Switch")
        self.root.geometry("460x520")
        self.root.minsize(420, 480)

        self.config = load_config()
        self.controller = create_wifi_controller()
        self.switching = False
        self.refreshing = False
        self.current_network = None

        self.current_var = tk.StringVar(value="Detecting…")
        self.current_note_var = tk.StringVar(value="")
        self.target_var = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="Ready")

        self.ui_queue = queue.Queue()

        self._build_ui()
        self.root.after(UI_QUEUE_MS, self._drain_ui_queue)
        self.refresh_state()
        self.root.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=28)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, text="Wi-Fi Switch", font=("TkDefaultFont", 22, "bold")
        ).pack(pady=(0, 30))

        ttk.Label(
            outer, text="Current Wi-Fi", font=("TkDefaultFont", 10)
        ).pack(anchor="w")
        ttk.Label(
            outer, textvariable=self.current_var,
            font=("TkDefaultFont", 17, "bold")
        ).pack(anchor="w", pady=(4, 2))

        ttk.Label(
            outer, textvariable=self.current_note_var, foreground="#666666"
        ).pack(anchor="w")

        ttk.Label(outer, text="↓", font=("TkDefaultFont", 24)).pack(pady=18)

        self.switch_button = ttk.Button(
            outer, text="SWITCH", command=self.switch_network
        )
        self.switch_button.pack(ipadx=45, ipady=14, pady=4)

        ttk.Label(outer, text="").pack(pady=3)

        ttk.Label(
            outer, text="Target Wi-Fi", font=("TkDefaultFont", 10)
        ).pack(anchor="w")
        ttk.Label(
            outer, textvariable=self.target_var,
            font=("TkDefaultFont", 17, "bold")
        ).pack(anchor="w", pady=(4, 18))

        status_frame = ttk.LabelFrame(outer, text="Status", padding=12)
        status_frame.pack(fill="x", pady=(8, 20))
        ttk.Label(
            status_frame, textvariable=self.status_var,
            wraplength=360
        ).pack(anchor="w")

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", side="bottom")

        ttk.Button(bottom, text="Settings", command=self.open_settings).pack(
            side="left"
        )
        self.refresh_button = ttk.Button(
            bottom, text="Refresh", command=self.refresh_state
        )
        self.refresh_button.pack(side="right")

    # ------------------------------------------------------------------
    # Thread plumbing
    # ------------------------------------------------------------------

    def _post(self, func, *args):
        """Schedule `func` on the UI thread from any thread.

        Tk calls are only safe from the thread running mainloop, so worker
        threads hand work over through this queue instead of calling
        root.after() themselves.
        """
        self.ui_queue.put((func, args))

    def _drain_ui_queue(self):
        while True:
            try:
                func, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args)
            except tk.TclError:
                return
        self.root.after(UI_QUEUE_MS, self._drain_ui_queue)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _auto_refresh(self):
        if not self.switching and not self.refreshing:
            self.refresh_state(quiet=True)
        self.root.after(AUTO_REFRESH_MS, self._auto_refresh)

    def refresh_state(self, quiet=False):
        if self.switching or self.refreshing:
            return

        self.refreshing = True
        self.refresh_button.state(["disabled"])
        if not quiet:
            self.status_var.set("Reading current Wi-Fi…")

        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            current = self.controller.get_current_network()
            error = None
        except Exception as exc:
            current, error = None, str(exc)
        self._post(self._apply_state, current, error)

    def _apply_state(self, current, error):
        self.refreshing = False
        self.refresh_button.state(["!disabled"])
        self.current_network = current

        if error:
            self.current_var.set("Unavailable")
            self.current_note_var.set("")
            self.target_var.set("—")
            self.status_var.set(f"Wi-Fi detection error: {error}")
            return

        self.current_var.set(current or "No Wi-Fi connection")
        self.current_note_var.set("Connected" if current else "Not associated")

        n1 = self.config.get("network1")
        n2 = self.config.get("network2")

        if not n1 or not n2:
            self.target_var.set("Configure two networks")
            self.status_var.set("Set up Network 1 and Network 2 in Settings.")
            return

        target = choose_target(current, n1, n2)
        self.target_var.set(target)

        if not self.switching and self.status_var.get() in (
            "Reading current Wi-Fi…", "Ready"
        ):
            self.status_var.set("Ready")

    # ------------------------------------------------------------------
    # Switching
    # ------------------------------------------------------------------

    def switch_network(self):
        if self.switching or self.refreshing:
            return

        n1 = self.config.get("network1")
        n2 = self.config.get("network2")

        if not n1 or not n2:
            messagebox.showinfo(
                "Setup required",
                "Configure Network 1 and Network 2 before switching."
            )
            self.open_settings()
            return

        self.switching = True
        self.switch_button.state(["disabled"])
        self.refresh_button.state(["disabled"])
        self.status_var.set("Checking current Wi-Fi…")

        threading.Thread(
            target=self._switch_worker,
            args=(n1, n2),
            daemon=True
        ).start()

    def _switch_worker(self, n1, n2):
        try:
            # Always re-read the live SSID: the switch direction depends on it,
            # and a stale value is what makes the app leave a network only to
            # rejoin the same one.
            current = self.controller.get_current_network()
            target = choose_target(current, n1, n2)

            if current == target:
                self._post(
                    self._switch_done,
                    f"Already connected to {target} — nothing to switch.",
                )
                return

            stored = credentials.get_password(target)
            self._ui_status(
                f"Connecting to {target}…"
                + (" (using saved password)" if stored else "")
            )
            try:
                self.controller.connect(target, password=stored)
            except JoinRefused as exc:
                password = self._ask_password(target, str(exc))
                if not password:
                    raise
                self._ui_status(f"Retrying {target} with password…")
                self.controller.connect(target, password=password)
                # Remember it so the next switch never has to interrupt.
                credentials.set_password(target, password)

            self._ui_status(f"Waiting for {target} to come up…")
            deadline = time.time() + VERIFY_TIMEOUT_S
            while time.time() < deadline:
                if self.controller.get_current_network() == target:
                    self._post(self._switch_done, f"Connected to {target}")
                    return
                time.sleep(VERIFY_INTERVAL_S)

            self._post(
                self._switch_done,
                f"Timed out waiting for {target} to associate.",
            )
        except Exception as exc:
            self._post(self._switch_done, f"Failed: {exc}")

    def _ui_status(self, message):
        self._post(self.status_var.set, message)

    def _ask_password(self, ssid, reason):
        """Prompt on the UI thread and block the worker until it is answered.

        The password is passed straight to the OS join command and is never
        written to the configuration file.
        """
        result = {}
        done = threading.Event()

        def prompt():
            try:
                result["value"] = simpledialog.askstring(
                    "Wi-Fi password",
                    f"macOS refused the join to '{ssid}'.\n"
                    f"{reason}\n\n"
                    "Enter the password to retry (not saved):",
                    show="*",
                    parent=self.root,
                )
            finally:
                done.set()

        self._post(prompt)
        done.wait(timeout=180)
        return result.get("value")

    def _switch_done(self, message):
        self.switching = False
        self.switch_button.state(["!disabled"])
        self.refresh_button.state(["!disabled"])
        self.status_var.set(message)
        self.refresh_state(quiet=True)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @staticmethod
    def _password_hint(ssid):
        if ssid and credentials.has_password(ssid):
            return f"Password saved for '{ssid}' — type here only to replace it."
        return "Password (optional) — leave blank to let macOS use its own."

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Wi-Fi Switch Settings")
        win.geometry("470x480")
        win.resizable(False, False)

        frame = ttk.Frame(win, padding=24)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text="Configure two Wi-Fi networks",
            font=("TkDefaultFont", 15, "bold")
        ).pack(anchor="w", pady=(0, 20))

        try:
            known = self.controller.get_saved_networks()
        except Exception:
            known = []

        saved_n1 = self.config.get("network1", "")
        saved_n2 = self.config.get("network2", "")

        ttk.Label(frame, text="Network 1").pack(anchor="w")
        n1 = tk.StringVar(value=saved_n1)
        ttk.Combobox(
            frame, textvariable=n1, values=known, width=43
        ).pack(fill="x", pady=(4, 6))

        p1 = tk.StringVar()
        ttk.Entry(frame, textvariable=p1, show="•", width=45).pack(
            fill="x", pady=(0, 4)
        )
        ttk.Label(
            frame,
            text=self._password_hint(saved_n1),
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 12))

        ttk.Label(frame, text="Network 2").pack(anchor="w")
        n2 = tk.StringVar(value=saved_n2)
        ttk.Combobox(
            frame, textvariable=n2, values=known, width=43
        ).pack(fill="x", pady=(4, 6))

        p2 = tk.StringVar()
        ttk.Entry(frame, textvariable=p2, show="•", width=45).pack(
            fill="x", pady=(0, 4)
        )
        ttk.Label(
            frame,
            text=self._password_hint(saved_n2),
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 12))

        where = (
            "the macOS keychain" if credentials.backend() == "keychain"
            else "memory for this run only"
        )
        hint = (
            "Passwords are optional — fill them in only if a switch keeps "
            f"getting refused. They are kept in {where}, never in the "
            "configuration file."
        )
        ttk.Label(
            frame, text=hint, foreground="#666666", wraplength=410
        ).pack(anchor="w", pady=(0, 16))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def save():
            a = n1.get().strip()
            b = n2.get().strip()
            if not a or not b:
                messagebox.showerror(
                    "Missing network",
                    "Both Network 1 and Network 2 are required.",
                    parent=win
                )
                return
            if a == b:
                messagebox.showerror(
                    "Duplicate network",
                    "Network 1 and Network 2 must be different.",
                    parent=win
                )
                return

            self.config = {"network1": a, "network2": b}
            save_config(self.config)

            # Blank means "leave whatever is already stored alone".
            if p1.get():
                credentials.set_password(a, p1.get())
            if p2.get():
                credentials.set_password(b, p2.get())

            win.destroy()
            self.refresh_state()

        def reset():
            if messagebox.askyesno(
                "Reset configuration",
                "Remove the saved Wi-Fi Switch configuration and any stored "
                "Wi-Fi passwords?",
                parent=win
            ):
                for ssid in (saved_n1, saved_n2, n1.get().strip(), n2.get().strip()):
                    credentials.delete_password(ssid)
                clear_config()
                self.config = {}
                win.destroy()
                self.refresh_state()

        def forget_passwords():
            for ssid in (saved_n1, saved_n2, n1.get().strip(), n2.get().strip()):
                credentials.delete_password(ssid)
            messagebox.showinfo(
                "Passwords cleared",
                "Stored Wi-Fi passwords for these networks were removed.",
                parent=win
            )

        ttk.Button(buttons, text="Save", command=save).pack(side="left")
        ttk.Button(
            buttons, text="Forget passwords", command=forget_passwords
        ).pack(side="left", padx=8)
        ttk.Button(buttons, text="Reset", command=reset).pack(side="right")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    WifiSwitchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
