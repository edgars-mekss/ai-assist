# Личный помощник — запуск. Первый раз скачивает в эту папку Ollama, модели Qwen 3.5 9B и GigaChat 3.1, голоса
# (Silero, Piper) и распознавание речи GigaAM (~17 ГБ), потом поднимает модель и сервер окна, открывает окно чата.
# Закрыли окно — всё останавливается (и напоминания тоже: окно можно сворачивать, но не закрывать).
# Всё лежит здесь (диск D): runtime\ — программы, models\ — модель, data\ — разговор и настройки.
#   start.ps1 -SetupOnly — только скачать всё нужное, окно не открывать
param([switch]$SetupOnly)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Rt = Join-Path $Root 'runtime'

# встроенные модели (как BUILTIN_MODELS в app/server.py): Qwen — по умолчанию, GigaChat — переключателем в окне.
# Свои модели добавляются в окне (⚙ → «Модели») и скачиваются оттуда.
$Models = 'qwen3.5:9b', 'hf.co/ai-sage/GigaChat3.1-10B-A1.8B-GGUF:Q4_K_M'
$OllamaPort = 11435   # не 11434 — чтобы не мешать Ollama, если она когда-то будет стоять отдельно
$AppPort = 8765
$OllamaZip = 'https://github.com/ollama/ollama/releases/download/v0.40.11/ollama-windows-amd64.zip'
$PiperZip = 'https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip'
$VoiceBase = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU'
$Voices = 'irina', 'dmitri', 'denis', 'ruslan'
$SileroUrl = 'https://models.silero.ai/models/tts/ru/v5_6_ru.pt'   # v5_6_ru: ударения, омографы, вопросы, восклицания

$env:OLLAMA_HOST = "127.0.0.1:$OllamaPort"
$env:OLLAMA_MODELS = Join-Path $Root 'models'
$env:ASSISTANT_PORT = "$AppPort"
$env:ASSISTANT_OLLAMA = "http://127.0.0.1:$OllamaPort"
$env:HF_HOME = Join-Path $Rt 'hf'   # распознавание речи GigaAM скачивается сюда

$Host.UI.RawUI.WindowTitle = 'Помощник — запуск'

function Say($text) { Write-Host $text -ForegroundColor Cyan }

$Tar = Join-Path $env:SystemRoot 'System32\tar.exe'   # именно системный: tar из Git не понимает путей вида D:\…

function Download($url, $file) {
    if (Test-Path $file) { return }   # уже скачан (прошлый запуск оборвался на распаковке)
    $part = "$file.part"
    & curl.exe -L --fail --retry 3 -C - --progress-bar -o $part $url
    if ($LASTEXITCODE -ne 0) { throw "Не скачалось: $url" }
    Move-Item -Force $part $file
}

function Wait-Url($url, $seconds) {
    for ($i = 0; $i -lt $seconds * 2; $i++) {
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $url | Out-Null; return $true } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

try {
    New-Item -ItemType Directory -Force $Rt, $env:OLLAMA_MODELS, (Join-Path $Root 'data') | Out-Null

    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Нет Python — поставьте Python 3 (python.org) и запустите снова.' }

    # --- Ollama (переносная сборка, без установки в систему) ---
    $Ollama = Join-Path $Rt 'ollama\ollama.exe'
    if (-not (Test-Path $Ollama)) {
        Say 'Скачиваю Ollama (~1,6 ГБ, один раз)…'
        $zip = Join-Path $Rt 'ollama.zip'
        Download $OllamaZip $zip
        New-Item -ItemType Directory -Force (Join-Path $Rt 'ollama') | Out-Null
        & $Tar -xf $zip -C (Join-Path $Rt 'ollama')
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Ollama)) { throw 'Не распаковалась Ollama' }
        Remove-Item $zip
    }

    # --- голос: Piper и русские голоса ---
    $Piper = Join-Path $Rt 'piper\piper.exe'
    if (-not (Test-Path $Piper)) {
        Say 'Скачиваю голосовой движок Piper…'
        $zip = Join-Path $Rt 'piper.zip'
        Download $PiperZip $zip
        & $Tar -xf $zip -C $Rt
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Piper)) { throw 'Не распаковался Piper' }
        Remove-Item $zip
    }
    $VoiceDir = Join-Path $Rt 'voices'
    New-Item -ItemType Directory -Force $VoiceDir | Out-Null
    foreach ($v in $Voices) {
        $name = "ru_RU-$v-medium.onnx"
        if (-not (Test-Path (Join-Path $VoiceDir $name))) {
            Say "Скачиваю голос: $v…"
            Download "$VoiceBase/$v/medium/$name.json" (Join-Path $VoiceDir "$name.json")
            Download "$VoiceBase/$v/medium/$name" (Join-Path $VoiceDir $name)
        }
    }

    # --- голос Silero v5 (русский): своё окружение Python с PyTorch для процессора, видеокарта остаётся модели ---
    $Py = Join-Path $Rt 'pyenv\Scripts\python.exe'
    if (-not (Test-Path $Py)) {
        Say 'Готовлю окружение для голоса Silero…'
        & python -m venv (Join-Path $Rt 'pyenv')
        if ($LASTEXITCODE -ne 0) { throw 'Не создалось окружение Python для Silero' }
    }
    & $Py -c 'import torch' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say 'Ставлю PyTorch для процессора (~250 МБ, один раз)…'
        $env:PIP_CACHE_DIR = Join-Path $Rt 'pip-cache'
        & $Py -m pip install --disable-pip-version-check torch numpy --index-url https://download.pytorch.org/whl/cpu
        if ($LASTEXITCODE -ne 0) { throw 'Не поставился PyTorch' }
    }
    $SileroModel = Join-Path $Rt 'silero\v5_6_ru.pt'
    if (-not (Test-Path $SileroModel)) {
        Say 'Скачиваю голос Silero v5 (~150 МБ)…'
        New-Item -ItemType Directory -Force (Join-Path $Rt 'silero') | Out-Null
        Download $SileroUrl $SileroModel
    }

    # --- ввод голосом: GigaAM v3 (русское распознавание, процессор) ---
    & $Py -c 'import onnx_asr' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say 'Ставлю распознавание речи (один раз)…'
        $env:PIP_CACHE_DIR = Join-Path $Rt 'pip-cache'
        & $Py -m pip install --disable-pip-version-check 'onnx-asr[cpu,hub]'
        if ($LASTEXITCODE -ne 0) { throw 'Не поставилось распознавание речи' }
    }
    if (-not (Test-Path (Join-Path $env:HF_HOME 'hub\models--istupakov--gigaam-v3-onnx'))) {
        Say 'Скачиваю модель распознавания GigaAM (~250 МБ)…'
        & $Py -c "import onnx_asr; onnx_asr.load_model('gigaam-v3-e2e-ctc', quantization='int8')"
        if ($LASTEXITCODE -ne 0) { throw 'Не скачалась модель распознавания' }
    }

    # --- модель ---
    $started = @()
    $ownOllama = $false   # Ollama уже работала (например, для NPC сервера GTA) — при выходе её не трогаем
    if (-not (Wait-Url "http://127.0.0.1:$OllamaPort/api/version" 1)) {
        Say 'Запускаю Ollama…'
        $ownOllama = $true
        $started += Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden -PassThru
        if (-not (Wait-Url "http://127.0.0.1:$OllamaPort/api/version" 60)) { throw 'Ollama не запустилась' }
    }
    $list = & $Ollama list 2>$null | Out-String
    foreach ($m in $Models) {
        if (-not $list.Contains($m)) {
            Say "Скачиваю модель $m (~6,5 ГБ, один раз)…"
            & $Ollama pull $m
            if ($LASTEXITCODE -ne 0) { throw "Не скачалась модель $m" }
        }
    }
    if ($SetupOnly) {
        foreach ($p in $started) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
        Say 'Всё скачано.'
        return
    }
    # --- сервер окна и само окно (модель он загружает заранее сам — первый ответ без долгого ожидания) ---
    # Python из pyenv: там голос Silero и распознавание речи.
    # Сервер, оставшийся от прошлого запуска, — остановить: иначе окно работает со старой версией (26.10 так пропал ввод голосом)
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -like "*$Root\app\server.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20; $i++) {   # дождаться, пока порт освободится
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 "http://127.0.0.1:$AppPort/api/state" | Out-Null; Start-Sleep -Milliseconds 250 } catch { break }
    }
    $started += Start-Process -FilePath $Py -ArgumentList @('-I', "`"$Root\app\server.py`"") -WindowStyle Hidden -PassThru
    if (-not (Wait-Url "http://127.0.0.1:$AppPort/api/state" 20)) { throw 'Сервер окна не запустился' }

    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    Say 'Готово. Окно чата открыто — закройте его, чтобы выключить помощника.'
    if ($edge) {
        $win = Start-Process -FilePath $edge -PassThru -ArgumentList @(
            "--app=http://127.0.0.1:$AppPort/", "--user-data-dir=`"$Rt\edge-profile`"", '--window-size=920,860',
            '--autoplay-policy=no-user-gesture-required', '--no-first-run', '--disable-features=Translate')
        $win.WaitForExit()
    } else {
        Start-Process "http://127.0.0.1:$AppPort/"
        Read-Host 'Открыто в браузере. Нажмите Enter здесь, чтобы выключить помощника'
    }

    foreach ($p in $started) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
    if ($ownOllama) {
        Get-Process ollama*, llama* -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Rt*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}
catch {
    Write-Host ''
    Write-Host "Ошибка: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Нажмите Enter, чтобы закрыть'
}
