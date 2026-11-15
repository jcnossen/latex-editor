"""Transcribe one voice note with faster-whisper (runs on the server's CPU; the audio never leaves the server).

    <venv>/bin/python transcribe.py <audio file> [--model small] [--models-dir DIR]
Prints JSON: {"text", "language", "duration", "seconds"} (seconds = processing time).
The model is downloaded once into --models-dir (default <server_root>/data/models).
"""
import argparse, json, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument('audio')
ap.add_argument('--model', default='small')
ap.add_argument('--models-dir', default=None)
ap.add_argument('--prompt', default=None, help='vocabulary hint, e.g. the jargon of the paper (config: whisper_prompt)')
a = ap.parse_args()

t = time.time()
models = a.models_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'models')
# every cache the model download uses stays in the models dir (the service user's home is not writable)
for k, sub in (('HF_HOME', 'hf_home'), ('XDG_CACHE_HOME', 'cache'), ('HF_XET_CACHE', 'xet')):
    os.environ[k] = os.path.join(models, sub)
from faster_whisper import WhisperModel  # noqa: E402

model = WhisperModel(a.model, device='cpu', compute_type='int8', download_root=models, cpu_threads=os.cpu_count() or 2)
segments, info = model.transcribe(a.audio, beam_size=5, vad_filter=True, initial_prompt=a.prompt or None)
text = ' '.join(s.text.strip() for s in segments).strip()
json.dump({'text': text, 'language': info.language, 'duration': round(info.duration, 1),
           'seconds': round(time.time() - t, 1)}, sys.stdout)
