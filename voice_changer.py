import sys
import os
import numpy as np
import sounddevice as sd
import queue
import tempfile
import subprocess
import soundfile as sf
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QSlider, QGroupBox, QFormLayout
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QColor, QPen

# ----------- Sophisticated VoiceGraph Widget -------------
class VoiceGraph(QWidget):
    def __init__(self):
        super().__init__()
        self.data = np.zeros(1024)
        self.setMinimumHeight(90)

    def update_waveform(self, data):
        if len(data) >= 1024:
            self.data = data[-1024:]
        else:
            self.data = np.pad(data, (1024 - len(data), 0))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        w, h = self.width(), self.height()
        pen = QPen(QColor(0, 255, 100), 2)
        painter.setPen(pen)
        if self.data is not None and np.max(np.abs(self.data)) > 0:
            x = np.linspace(0, w, len(self.data))
            y = h / 2 - self.data / np.max(np.abs(self.data)) * h / 2
            points = [Qt.QPointF(x[i], y[i]) for i in range(len(x))]
            painter.drawPolyline(*points)

# ----------- Main Voice Changer GUI ----------------------
class VoiceChanger(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Voice Changer (so-vits-svc powered)")
        self.setMinimumSize(950, 700)
        self.target_voice_path = None
        self.pitch_shift = 0
        self.running = False
        self.input_audio = np.zeros(1024)
        self.transformed_audio = np.zeros(1024)
        self.modified_audio = np.zeros(1024)
        self.stream = None

        # so-vits-svc model config (!!! SET THESE BEFORE USE !!!)
        self.SVC_INFER_SCRIPT = "/home/ntb/so-vits-svc/inference_main.py" # path to so-vits-svc inference_main.py
        self.CONFIG_PATH = "/path/to/config.json"                       # path to model config.json
        self.MODEL_PATH = "/path/to/model.pth"                          # path to model .pth
        self.SPEAKER_ID = 0                                             # set the correct speaker id for your model

        self.init_ui()
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh_graphs)
        self.timer.start(30)

    def init_ui(self):
        main = QWidget()
        layout = QVBoxLayout(main)

        # Top: Target voice upload
        row = QHBoxLayout()
        self.target_file_label = QLabel("No target loaded")
        load_btn = QPushButton("Load Target Voice (for speaker id)")
        load_btn.clicked.connect(self.load_target_voice)
        row.addWidget(load_btn)
        row.addWidget(self.target_file_label)
        layout.addLayout(row)

        # --- Graphs ---
        graphs_row = QHBoxLayout()
        self.in_graph = VoiceGraph()
        self.mod_graph = VoiceGraph()
        self.tr_graph = VoiceGraph()
        col1 = QVBoxLayout()
        col1.addWidget(QLabel("Incoming Voice"))
        col1.addWidget(self.in_graph)
        col2 = QVBoxLayout()
        col2.addWidget(QLabel("Modifying Incoming Voice"))
        col2.addWidget(self.mod_graph)
        col3 = QVBoxLayout()
        col3.addWidget(QLabel("Transformed Voice"))
        col3.addWidget(self.tr_graph)
        graphs_row.addLayout(col1)
        graphs_row.addLayout(col2)
        graphs_row.addLayout(col3)
        layout.addLayout(graphs_row)

        # --- Controls ---
        controls = QGroupBox("Voice Controls")
        controls_layout = QFormLayout()
        self.pitch_slider = QSlider(Qt.Horizontal)
        self.pitch_slider.setRange(-12, 12)
        self.pitch_slider.setValue(0)
        self.pitch_slider.valueChanged.connect(self.change_pitch)
        controls_layout.addRow("Pitch Shift (semitones)", self.pitch_slider)
        controls.setLayout(controls_layout)
        layout.addWidget(controls)

        # --- Multi-speaker (Placeholder) ---
        ms_box = QGroupBox("Multi-Speaker Management")
        ms_layout = QHBoxLayout()
        for s in range(1, 3):
            btn = QPushButton(f"Speaker {s}")
            btn.setEnabled(False)
            ms_layout.addWidget(btn)
        ms_box.setLayout(ms_layout)
        layout.addWidget(ms_box)

        # --- LIVE mode, Export, Operation Bar ---
        bottom_row = QHBoxLayout()
        self.live_btn = QPushButton("Start LIVE Mode")
        self.live_btn.setCheckable(True)
        self.live_btn.clicked.connect(self.toggle_live_mode)
        export_btn = QPushButton("Export Transformed")
        export_btn.clicked.connect(self.export_audio)
        bottom_row.addWidget(self.live_btn)
        bottom_row.addWidget(export_btn)
        layout.addLayout(bottom_row)

        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("Operation:"))
        self.op_play = QPushButton("▶ Play")
        self.op_play.clicked.connect(self.play_last_audio)
        self.op_save = QPushButton("💾 Save")
        self.op_save.clicked.connect(self.export_audio)
        self.op_clear = QPushButton("🗑 Clear")
        self.op_clear.clicked.connect(self.clear_audio)
        op_row.addWidget(self.op_play)
        op_row.addWidget(self.op_save)
        op_row.addWidget(self.op_clear)
        layout.addLayout(op_row)
        self.setCentralWidget(main)

    def load_target_voice(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Choose Target Voice", "", "Audio files (*.wav *.mp3 *.flac)")
        if file_path:
            self.target_file_label.setText(file_path.split("/")[-1])
            self.target_voice_path = file_path
            # For so-vits-svc, set self.SPEAKER_ID accordingly if your model is multi-speaker

    def change_pitch(self, value):
        self.pitch_shift = value

    def toggle_live_mode(self):
        if self.live_btn.isChecked():
            self.live_btn.setText("Stop LIVE Mode")
            self.running = True
            self.start_live_audio()
        else:
            self.live_btn.setText("Start LIVE Mode")
            self.running = False
            if self.stream: self.stream.stop()

    def start_live_audio(self):
        def callback(indata, outdata, frames, time, status):
            if not self.running:
                outdata[:] = np.zeros_like(indata)
                return
            self.input_audio = indata[:, 0]
            self.mod_graph.update_waveform(self.input_audio)
            transformed = self.transform_voice(self.input_audio, self.pitch_shift)
            outdata[:, 0] = transformed[:len(outdata)]
            self.transformed_audio = transformed
            self.tr_graph.update_waveform(self.transformed_audio)
            self.in_graph.update_waveform(self.input_audio)
        self.stream = sd.Stream(channels=1, samplerate=16000, blocksize=1024, callback=callback)
        self.stream.start()

    def transform_voice(self, x, pitch_shift):
        """
        True so-vits-svc inference: 
        Save the input as WAV, call so-vits-svc inference_main.py, load result.
        """
        # Save input to temp WAV file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
            sf.write(tmp_in.name, x, 16000)
            input_path = tmp_in.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            output_path = tmp_out.name

        # Compose the so-vits-svc inference command
        command = [
            sys.executable, self.SVC_INFER_SCRIPT,
            "-c", self.CONFIG_PATH,
            "-m", self.MODEL_PATH,
            "-n", input_path,
            "-o", output_path,
            "-spk", str(self.SPEAKER_ID),
            "--transpose", str(pitch_shift)
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            y, sr = sf.read(output_path)
            if sr != 16000:
                import librosa
                y = librosa.resample(y, orig_sr=sr, target_sr=16000)
            # Clean up temp files
            os.remove(input_path)
            os.remove(output_path)
            return y[:1024] if len(y) > 1024 else np.pad(y, (0, 1024 - len(y)))
        except Exception as e:
            print("Voice conversion error:", e)
            return np.zeros(1024)

    def refresh_graphs(self):
        self.in_graph.update_waveform(self.input_audio)
        self.tr_graph.update_waveform(self.transformed_audio)
        self.mod_graph.update_waveform(self.input_audio)

    def play_last_audio(self):
        sd.play(self.transformed_audio, samplerate=16000)

    def export_audio(self):
        fname, _ = QFileDialog.getSaveFileName(self, "Save transformed", "", "WAV (*.wav)")
        if fname:
            sf.write(fname, self.transformed_audio, 16000)
            print(f"Saved: {fname}")

    def clear_audio(self):
        self.input_audio = np.zeros(1024)
        self.transformed_audio = np.zeros(1024)
        self.mod_graph.update_waveform(self.input_audio)
        self.in_graph.update_waveform(self.input_audio)
        self.tr_graph.update_waveform(self.input_audio)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = VoiceChanger()
    win.show()
    sys.exit(app.exec())
