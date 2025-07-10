import sys
import os
import threading
import time
import traceback
import json
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QColorDialog, QMessageBox, QInputDialog
)
from PySide6.QtCore import Qt, Signal, QTimer, QThread

from tgcrypto import ige256_encrypt, ige256_decrypt
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from PySide6.QtGui import QTextCursor
import discord
from discord.ext import tasks
import tgcrypto
from Crypto.Cipher import AES
import discord
import nest_asyncio

# --- AES-256-GCM Parameters ---
GCM_KEY = bytes.fromhex("adf0de6ffdf5484bd03f34264c1ed536646afd85eeaf6206fc7da262d7cf660f")
GCM_IV  = bytes.fromhex("9569eece25da9445e3864e2a")  # 12 bytes for GCM

# --- AES-256-IGE Parameters ---
IGE_KEY = bytes.fromhex("64177f070bd33f66971ece6b61e5e71af1693e1e348f79cdd679b16c07329a86")
IGE_IV  = bytes.fromhex("04af51b262ae169a499378397805feb097165222b0b582870be50ffa97d21053")

# --- Discord Hardcoded credentials ---
DISCORD_TOKEN = ""  # CHANGE THIS!
DISCORD_CHANNEL_ID =   # CHANGE TO YOUR CHANNEL ID!

# ---- ENCRYPTION FUNCTIONS ----
def pad(data):
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len] * pad_len)

def unpad(data):
    pad_len = data[-1]
    if not 1 <= pad_len <= 16:
        raise ValueError("Invalid padding")
    return data[:-pad_len]

def aes_gcm_encrypt(plaintext):
    cipher = AES.new(GCM_KEY, AES.MODE_GCM, nonce=GCM_IV)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return ciphertext, tag

def aes_gcm_decrypt(ciphertext, tag):
    cipher = AES.new(GCM_KEY, AES.MODE_GCM, nonce=GCM_IV)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    return plaintext

def combo_encrypt(username, message):
    # Encrypt username separately for info (not shown in chat)
    enc_username = aes_gcm_encrypt(username.encode())[0].hex()
    data = f"{enc_username}|{message}"
    gcm_ciphertext, tag = aes_gcm_encrypt(data.encode())
    ige_input = pad(gcm_ciphertext + tag)
    ige_ciphertext = ige256_encrypt(ige_input, IGE_KEY, IGE_IV)
    return ige_ciphertext.hex()

def combo_decrypt(hex_input):
    ige_ciphertext = bytes.fromhex(hex_input)
    ige_plain = ige256_decrypt(ige_ciphertext, IGE_KEY, IGE_IV)
    gcm_output = unpad(ige_plain)
    gcm_ciphertext, tag = gcm_output[:-16], gcm_output[-16:]
    plain = aes_gcm_decrypt(gcm_ciphertext, tag)
    s = plain.decode(errors='replace')
    parts = s.split("|", 1)
    if len(parts) == 2:
        enc_username, msg = parts
        try:
            username = aes_gcm_decrypt(bytes.fromhex(enc_username), b"\x00"*16)
            username = username.decode(errors='replace')
        except Exception:
            username = "<error>"
        return username, msg
    else:
        return "<unknown>", s

def format_message(timestamp, username, message, self_user, user_colors):
    # Chat color logic: self - grey, others - light purple, system - orange
    if username == "[SYSTEM]":
        color = "#FFA500"
    elif username == self_user:
        color = "#888888"
    else:
        color = user_colors.get(username, "#D6BAF6")
    html = f'<div style="color:{color}">[{timestamp}] &#123;<b>{username}</b>&#125; : <b>{message}</b></div>'
    return html

# ---- DISCORD LISTENER THREAD ----
class DiscordListener(QThread):
    message_received = Signal(str, str, str)  # timestamp, username, message
    error_signal = Signal(str)
    reconnecting_signal = Signal()

    def __init__(self, token, channel_id, self_user):
        super().__init__()
        self.token = token
        self.channel_id = channel_id
        self.self_user = self_user
        self.client = None
        self.running = True

    def run(self):
        intents = discord.Intents.default()
        intents.message_content = True

        class DiscordBot(discord.Client):
            async def on_ready(botself):
                try:
                    channel = botself.get_channel(self.channel_id)
                    if channel:
                        messages = [msg async for msg in channel.history(limit=30, oldest_first=True)]
                        for msg in messages:
                            if msg.author.bot:
                                try:
                                    username, payload = combo_decrypt(msg.content.strip())
                                    ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                                    self.message_received.emit(ts, username, payload)
                                except Exception:
                                    continue
                except Exception as e:
                    self.error_signal.emit(f"Error fetching history: {e}")

            async def on_message(botself, message):
                if message.author.bot:
                    try:
                        username, payload = combo_decrypt(message.content.strip())
                        ts = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        self.message_received.emit(ts, username, payload)
                    except Exception as e:
                        self.error_signal.emit(f"Error decrypting message: {e}")

        while self.running:
            try:
                self.client = DiscordBot(intents=intents)
                self.client.channel_id = self.channel_id
                self.client.run(self.token)
            except Exception as e:
                self.error_signal.emit(f"Discord error: {e}")
                self.reconnecting_signal.emit()
                time.sleep(5)

    def stop(self):
        self.running = False
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

# ---- MAIN GUI ----
class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VORTEX CHAT")
        self.resize(750, 600)
        self.username = self.get_username()
        self.user_colors = {}
        self.init_ui()
        self.discord_thread = DiscordListener(DISCORD_TOKEN, DISCORD_CHANNEL_ID, self.username)
        self.discord_thread.message_received.connect(self.handle_new_message)
        self.discord_thread.error_signal.connect(self.handle_error)
        self.discord_thread.reconnecting_signal.connect(self.handle_reconnect)
        self.discord_thread.start()
        self.show_system_msg("Welcome! Type your message below. Type 'x' to return to lobby.")
        self.last_sent = None

    def closeEvent(self, event):
        self.discord_thread.stop()
        event.accept()

    def get_username(self):
        settings_file = os.path.expanduser("~/.chat3")
        if os.path.exists(settings_file):
            with open(settings_file, "r") as f:
                username = f.read().strip()
                if username:
                    return username
        username, ok = QInputDialog.getText(self, "Set Username", "Enter your username:")
        if ok and username.strip():
            with open(settings_file, "w") as f:
                f.write(username.strip())
            return username.strip()
        return "anon"

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        layout.addWidget(self.chat_display)
        h = QHBoxLayout()
        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("Enter your message (or 'x' to exit to lobby)...")
        self.input_line.returnPressed.connect(self.send_message)
        h.addWidget(self.input_line)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_message)
        h.addWidget(self.send_btn)
        self.set_btn = QPushButton("Set Username")
        self.set_btn.clicked.connect(self.set_username)
        h.addWidget(self.set_btn)
        layout.addLayout(h)
        self.clear_btn = QPushButton("Destroy All Messages")
        self.clear_btn.clicked.connect(self.clear_messages)
        h.addWidget(self.clear_btn)

    def show_system_msg(self, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html = format_message(ts, "[SYSTEM]", msg, self.username, self.user_colors)
        self.chat_display.append(html)

    def handle_new_message(self, timestamp, username, message):
        if username not in self.user_colors and username != self.username and username != "[SYSTEM]":
            color_list = ["#D6BAF6", "#B5D6F6", "#F6BAD9", "#F6F0BA"]
            idx = len(self.user_colors) % len(color_list)
            self.user_colors[username] = color_list[idx]
        html = format_message(timestamp, username, message, self.username, self.user_colors)
        self.chat_display.append(html)
        self.chat_display.moveCursor(self.chat_display.textCursor().End)

    def handle_error(self, msg):
        self.show_system_msg(f"Error: {msg}")

    def handle_reconnect(self):
        self.show_system_msg("Reconnecting to 🛜...")

    def set_username(self):
        username, ok = QInputDialog.getText(self, "Set Username", "Enter new username:")
        if ok and username.strip():
            self.username = username.strip()
            settings_file = os.path.expanduser("~/.discord_gui_username")
            with open(settings_file, "w") as f:
                f.write(self.username)
            self.show_system_msg(f"Changed username to {self.username}")
            self.send_discord_message(f"[SYSTEM] {self.username} changed their username.")

    def clear_messages(self):
        try:
            self.send_discord_message("[SYSTEM] All previous messages destroyed by user!")
            self.chat_display.clear()
        except Exception:
            self.show_system_msg("Error destroying messages!")

    def send_message(self):
        msg = self.input_line.text().strip()
        if not msg:
            return
        if msg == "x":
            self.show_system_msg("Returned to lobby (close app to exit).")
            self.input_line.clear()
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = f"[{timestamp}] {{{self.username}}} : {msg}"
        try:
            self.send_discord_message(payload)
            self.show_system_msg("Message Sent!")
        except Exception as e:
            self.show_system_msg(f"Error sending message! ({e})")
        self.input_line.clear()

    def send_discord_message(self, message):
        enc_username = aes_gcm_encrypt(self.username.encode())[0].hex()
        payload = f"{enc_username}|{message}"
        gcm_ciphertext, tag = aes_gcm_encrypt(payload.encode())
        ige_input = pad(gcm_ciphertext + tag)
        ige_ciphertext = ige256_encrypt(ige_input, IGE_KEY, IGE_IV)
        payload_hex = ige_ciphertext.hex()
        def sender():
            try:
                import asyncio
                nest_asyncio.apply()
                intents = discord.Intents.default()
                intents.message_content = True
                class TinyClient(discord.Client):
                    async def on_ready(self):
                        try:
                            channel = self.get_channel(DISCORD_CHANNEL_ID)
                            if channel:
                                await channel.send(payload_hex)
                        except Exception as e:
                            pass
                        await self.close()
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                client = TinyClient(intents=intents)
                client.run(DISCORD_TOKEN)
            except Exception as e:
                self.show_system_msg(f"Error sending message! {e}")
        threading.Thread(target=sender).start()

# Aplikace start
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ChatWindow()
    win.show()
    sys.exit(app.exec())
