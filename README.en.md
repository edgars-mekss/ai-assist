# Pomoshnik — an offline voice AI assistant for Windows

> **Status: early work in progress.** Built in about two months with Claude Code and Cursor.

A personal AI assistant that runs **entirely on your own PC, with no internet connection after setup**. You talk to it by voice or text. It sets reminders, tracks habits, remembers what matters about you, and can carry out tasks on your computer. Every risky action needs your explicit approval.

The interface is in Russian. The models, speech recognition and voices are tuned for Russian.

## Features

- **Streaming chat** with local LLMs (Qwen 3.5 9B on GPU, GigaChat 3.1 on CPU). You can switch models in the header and install any open model from Ollama or Hugging Face from the UI.
- **Voice in.** Push-to-talk (Ctrl+Space). The phrase is sent after 1.3 s of silence and recognized locally by GigaAM in about 0.1 s.
- **Voice out.** The reply is spoken sentence by sentence while it is still being generated (Silero or Piper voices).
- **Reminders.** Set them in plain language ("remind me tomorrow at 9 to call mom") and get Windows notifications.
- **Habit tracker.** "Ran 5 km today" checks off the habit and keeps your streak. In the evening the assistant asks about habits you haven't checked yet.
- **Long-term memory.** The assistant extracts facts about you in the background, updates them when they change, and lets you edit or wipe them.
- **Search over past conversations.** BM25 over the full archive, including cleared chats.
- **Tasks on your PC.** System info, file search and reading in allowed folders, opening apps and sites, screenshots, creating files, sending email.

## Safety by design

The assistant can act on your computer, so safety was designed in from the start:

- **No destructive tools at all.** It cannot delete, move or overwrite files, run commands or change system settings. A new file never replaces an existing one.
- **Human-in-the-loop confirmation.** Email, opening things, creating files and screenshots require a click on "Allow". The confirmation card shows the full action. The card id is known only to the UI, so the model cannot approve its own request. No answer within 5 minutes means "no".
- **Per-tool permissions:** allowed / ask / denied, plus a master switch.
- **Prompt-injection resistance.** Text from files, emails and the screen is passed to the model as data, not instructions. This was tested with a planted file that said "send an email to evil@…".
- **Folder sandbox.** The assistant only touches allowed folders. Secrets (`.env`, `.ssh`, keys, tokens), `AppData`, `Windows` and `Program Files` are always blocked.
- **Hardened local server.** It rejects foreign `Host` and `Origin` headers and non-JSON requests, which protects it from malicious websites in the browser.
- **Encrypted credentials.** The mail password is encrypted with Windows DPAPI and is never sent back to the UI.
- **Audit log** of every action.

## Measured, not guessed

Tool-calling accuracy on 8 "I did something useful" phrases plus 4 neutral ones (26 Oct):

| Model | Habit checked | False positives | Latency |
|---|---|---|---|
| Qwen 9B, no reasoning | 1 / 8 | 0 | 1.5 s |
| Qwen 9B, reasoning | 6 / 8 | 0 | 4.7 s |
| GigaChat 3.1 | 8 / 8 | 0 | 2.1 s |

Other decisions based on testing:
- **Prompt ordering is tuned to Ollama's prompt cache.** The stable part (persona, memory, tool rules) comes first. Volatile context (time, reminders) goes last for CPU models, because a re-read costs about 10 s, and first for GPU models, because Qwen stops calling tools when that line sits mid-conversation.
- **History is capped** at 10k characters / 40 messages. Longer contexts made the model reason itself into silence. Older messages stay searchable in the archive.
- **Empty-answer fallback.** If the reasoning model writes its answer inside its reasoning and returns nothing, the step is retried without reasoning.

## Architecture

| Component | Role |
|---|---|
| `start.ps1` | One-click setup and launch. Downloads what's missing (~17 GB) into the project folder and installs nothing system-wide. Starts Ollama, the local server and an Edge app window, and shuts everything down when the window closes. |
| `app/server.py` | Local HTTP server (`127.0.0.1:8765`): chat streaming, TTS, STT, settings, history, memory, reminders, habits, model management |
| `app/agent.py` | PC tools, permissions, confirmation flow, SMTP mail, audit log |
| `app/tools.py` | Reminders and habits scheduler, Windows notifications via WinRT, archive search (BM25 with stemming), speech recognition |
| `app/silero_worker.py` | Silero TTS in a separate process |
| `app/web/` | Chat UI in plain HTML, CSS and JS: streaming markdown, TTS queue, 16 kHz WAV mic capture, side panels |

**Stack:** Python, JavaScript, HTML/CSS, PowerShell · Ollama · Qwen 3.5, GigaChat 3.1 · GigaAM v3 (ONNX) · Silero v5, Piper

## Run

Windows 10/11. Double-click `Запустить помощника.cmd`. The first launch downloads about 17 GB; after that everything works offline.

## Licensing notes

The Silero voice model is licensed for **non-commercial use only**. Of the Piper voices, only Dmitri and Denis (CC0) are suitable for commercial use. Models and voices are downloaded at first run and are not included in this repository.

## Roadmap

- [ ] Phone companion
- [ ] Demo video and screenshots
- [ ] English UI
