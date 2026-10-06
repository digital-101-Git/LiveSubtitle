# LiveSubtitle

음성 인식·번역을 실시간 오버레이 자막으로 표시하는 Windows 앱입니다.

[GitHub 저장소](https://github.com/digital-101-Git/LiveSubtitle) · [개발 환경·빌드 안내](SOURCE_RELEASE.txt)

사용자 매뉴얼 · User manuals: [한국어](<0_UserManual(사용자매뉴얼)/사용자매뉴얼.md>) · [English](<0_UserManual(사용자매뉴얼)/UserManual.md>) · [中文](<0_UserManual(사용자매뉴얼)/用户手册.md>) · [日本語](<0_UserManual(사용자매뉴얼)/ユーザーマニュアル.md>)

## 소개 · 한국어

LiveSubtitle은 Chrome·Edge에서 재생하는 방송, PC의 시스템 소리 또는 마이크 음성을 인식하고 번역한 뒤, 화면 위에 자막으로 표시하는 Windows 앱입니다. 브라우저 확장 프로그램 없이 사용할 수 있습니다.

### 사용 조건

- 사용 전 **시스템 메모리(RAM)를 10GB 이상 여유로 확보**하세요. 설치된 총용량이 아니라, 다른 프로그램이 사용 중인 메모리를 제외한 여유 공간 기준입니다.
- **NVIDIA GPU가 필수**입니다. 현재 로컬 추론은 CUDA를 사용하며, 테스트에는 **GeForce RTX 4080 16GB**를 사용했습니다.
- 필요한 RAM과 GPU 메모리(VRAM)는 선택한 음성 인식·번역 모델에 따라 달라집니다. 게임이나 방송 프로그램과 함께 실행하려면 메모리와 GPU 성능에 추가 여유가 필요합니다.

### 주요 사용처

- **인터넷 방송 시청:** 외국어 방송의 음성을 실시간으로 인식·번역해 오버레이 자막으로 표시합니다.
- **YouTube 시청:** 영상의 음성을 받아 원문 또는 번역 자막을 표시합니다.
- **마이크 음성 자막:** 마이크로 입력한 말을 인식·번역해 모니터에 오버레이로 표시합니다. 스트리머도 활용할 수 있지만, 방송과 함께 처리할 수 있는 메모리·GPU 여유가 필요합니다.
- **영상·강의·팟캐스트 시청:** 시스템 전체 소리를 입력으로 선택해 PC에서 재생되는 콘텐츠의 자막을 표시합니다.
- **같은 언어로 받아쓰기:** 입력 언어와 번역 언어를 같게 설정하면, 번역 없이 인식한 원문을 자막으로 표시합니다.

> [!WARNING]
> **음성 인식과 번역 처리로 인해 자막은 실제 음성보다 약 5초 늦게 표시됩니다.**
> 지연 시간은 모델, PC 성능, 발화 길이와 동시 실행 프로그램에 따라 달라질 수 있습니다. 영상 재생을 지연시켜 자막과 자동으로 맞추는 기능은 없습니다.
>
> **배경음악·효과음이 음성과 겹치거나, 잡음·왜곡 등으로 오디오 품질이 좋지 않으면 음성 인식 정확도가 크게 떨어질 수 있습니다.**
> 이 경우 대사가 누락되거나 잘못 인식되어, 원문과 번역 자막이 부정확하게 표시될 수 있습니다.

### 동작 방식과 주요 기능

기본 로컬 모드에서는 음성 인식과 번역을 모두 PC에서 처리합니다. 음성 인식은 Qwen3-ASR 또는 Whisper, 번역은 GGUF 모델을 사용하는 llama.cpp가 담당합니다. 선택 기능인 Gemini Live를 사용하면 음성 인식용 오디오가 Google로 전송되며 API 키가 필요합니다. 이 경우에도 번역은 로컬 모델에서 처리합니다.

- **입력·번역 언어:** 한국어, 영어, 중국어, 일본어. 입력은 자동 감지도 지원하며 번역 언어의 기본값은 한국어입니다. 실제 번역 가능 언어는 선택한 모델에 따라 다릅니다.
- **화면 언어:** 상단 `Language`에서 한국어·English·中文·日本語를 선택합니다. 메뉴와 입력·번역 언어 목록의 표시 이름이 바뀌며, 선택한 음성 인식·번역 언어는 유지됩니다.
- **자막 표시:** 브라우저 창 또는 모니터에 표시하고, 위치·크기를 편집할 수 있습니다. 최근 두 구절을 위·아래로 갱신합니다.
- **음성 확정 방식:** 기본, 반복확인(느림), Whisper AlignAtt(실험)를 선택할 수 있습니다. 반복 확인이 모든 영상에서 정확도를 높이는 것은 아닙니다.
- **기록:** 최근 원문과 번역문을 복사할 수 있으며, 최대 100개의 번역 기록을 로컬에 보관합니다. 앱에서 기록을 삭제할 수 있습니다.
- **모델 관리:** 모델은 앱의 `models` 폴더에 두고 선택합니다. Ollama는 필요하지 않습니다. 전용 번역 모델은 해당 입력 형식에 맞는 앱 지원이 필요합니다.

브라우저 오디오는 선택한 창의 프로세스와 자식 프로세스를 기준으로 캡처합니다. 같은 브라우저의 다른 탭 소리도 포함될 수 있습니다. 이 앱은 영상 재생을 지연시켜 자막과 동기화하지 않으며, 음성 구간 확정과 모델 처리에 따른 자막 지연이 발생합니다.

이 프로그램은 ChatGPT 6 Astra Ultra로 제작되었습니다.

## Introduction · English

LiveSubtitle is a Windows application that transcribes and translates audio from Chrome, Edge, the system mix, or a microphone, then displays the translation as an on-screen subtitle overlay. No browser extension is required.

### Requirements

- Keep **at least 10GB of system memory (RAM) available** before starting the app. This means memory left free after other applications are running, not total installed RAM.
- **An NVIDIA GPU is required.** Local inference currently uses CUDA. Testing was performed with a **GeForce RTX 4080 16GB**.
- RAM and GPU memory (VRAM) requirements depend on the selected speech recognition and translation models. Running games or broadcasting software alongside the app requires additional memory and GPU capacity.

### Use cases

- **Live streams:** Transcribe and translate foreign-language broadcasts into subtitle overlays.
- **YouTube videos:** Display original-language or translated captions from the video's audio.
- **Microphone captions:** Transcribe or translate microphone input and display it as a monitor overlay. Streamers can also use this, provided there is enough memory and GPU capacity to run it alongside broadcasting software.
- **Videos, lectures, and podcasts:** Select all system audio to caption content playing on the PC.
- **Same-language transcription:** Set the input and translation languages to the same language to display the recognized original text without translation.

> [!WARNING]
> **Speech recognition and translation introduce approximately 5 seconds of delay between the audio and the displayed captions.**
> Actual latency varies with the models, PC performance, speech length, and other running applications. The app does not delay video playback to synchronize it with captions.
>
> **Background music or sound effects overlapping speech, as well as poor audio quality caused by noise or distortion, can significantly reduce speech recognition accuracy.**
> Speech may be missed or misrecognized, resulting in inaccurate transcriptions and translated subtitles.

### How it works and key features

In local mode, both speech recognition and translation run on your PC. The app uses Qwen3-ASR or Whisper for recognition and llama.cpp with GGUF models for translation. Optional Gemini Live recognition sends audio to Google and requires an API key; translation still runs locally.

The interface supports Korean, English, Chinese, and Japanese as input and output choices, with automatic input-language detection and Korean as the default translation language. Supported translation directions depend on the selected model. The overlay keeps the latest two caption segments, supports position and size editing, and provides a local history of up to 100 translations.

The `Language` selector switches the interface between Korean, English, Chinese, and Japanese. It also localizes the input and output language names without changing your selected recognition or translation language. The interface preference is saved for the next launch.

Browser capture follows a process tree rather than a single tab, so audio from other tabs may be included. Subtitles have processing latency, and the app does not delay video playback to synchronize them. A GGUF file appearing in the model list does not guarantee compatibility with its required translation prompt.

This application was developed using ChatGPT 6 Astra Ultra.

## 시작하기

검증 환경은 **Windows 11 x64 · NVIDIA GeForce RTX 4080 16GB**입니다. 이는 검증에 사용한 환경이며 최소 사양을 뜻하지 않습니다. 현재 로컬 추론 경로는 NVIDIA CUDA를 기준으로 구성되어 있습니다.

일반 사용자용 실행본은 [Releases](https://github.com/digital-101-Git/LiveSubtitle/releases)에 별도로 제공할 예정입니다. GitHub의 **Code → Download ZIP**은 소스 코드이며, 바로 실행할 수 있는 앱 패키지가 아닙니다. 소스로 실행하려면 [.NET 10 SDK·Python 3.12·llama.cpp 구성 및 빌드 안내](SOURCE_RELEASE.txt)를 따르세요. `build.ps1`은 Windows 앱을 빌드하며 Python 환경이나 모델을 자동 설치하지 않습니다.

실행본과 필요한 런타임이 준비된 경우:

1. 아래에서 음성 인식 모델 하나와 번역 모델을 다운로드하여 지정된 폴더에 넣습니다.
2. `LiveSubtitle.exe`를 실행합니다.
3. 오디오 소스, 음성 인식 모델, 번역 모델, 입력 언어와 번역 언어를 선택합니다.
4. **모델 준비** 후 **실시간 자막 시작**을 누릅니다.

자세한 조작 방법은 [사용자 매뉴얼](<0_UserManual(사용자매뉴얼)/사용자매뉴얼.md>)을 참고하세요.

### 기본 모델 다운로드와 저장 위치

경로는 `LiveSubtitle.exe`가 있는 앱 폴더 기준입니다. 모델 가중치는 소스 저장소에 포함하지 않습니다.

| 역할 | 모델 | 다운로드 | 저장할 폴더·파일 |
| --- | --- | --- | --- |
| 음성 인식 · 선택 1 | Qwen3-ASR-1.7B | [Transformers 형식 모델 폴더](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf/tree/bcd2b5b7f32b480ab5790554cfa8347f246a14f3) | `models/asr/qwen3-asr-1.7b/` — `model.safetensors` |
| 음성 인식 · 선택 2 | Whisper large-v3-turbo | [faster-whisper / CTranslate2 모델 폴더](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/tree/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf) | `models/asr/whisper-large-v3-turbo/` — `model.bin` |
| 번역 | HY-MT2-7B · Q6_K | [GGUF 다운로드](https://huggingface.co/tencent/Hy-MT2-7B-GGUF/resolve/ab8472660ac61fac25f1af43fac2599d52a8a775/HY-MT2-7B-Q6_K.gguf?download=true) | `models/translation/HY-MT2-7B-Q6_K.gguf` |

이 저장소에는 위 두 ASR 모델의 설정·토크나이저와 원문 고지가 포함되어 있습니다. 해당 버전에 맞는 위 가중치를 추가하세요. 다른 버전이나 모델을 사용한다면 필요한 부속 파일도 함께 준비합니다. Whisper의 `model.bin`은 기본 인식용이고, `.pt` 파일은 AlignAtt 실험 기능용으로 구분합니다.

번역 GGUF는 **원래 파일명을 유지**하세요. 현재 앱은 알려진 모델의 파일명으로 전용 번역 방식을 선택합니다. 검증된 추가 모델과 저장 방법은 [매뉴얼의 모델 목록](<0_UserManual(사용자매뉴얼)/사용자매뉴얼.md#검증된-모델-다운로드>)에 있습니다. 임의의 GGUF를 추가하면 모두 정상 번역되는 구조는 아닙니다. `ja-ko-vn-12b-v2`는 일본어→한국어 전용입니다.

## 구조

음성 입력부터 출력까지의 기본 흐름입니다. Gemini를 선택하면 음성 인식 단계만 클라우드로 바뀝니다.

```text
브라우저 / 시스템 소리 / 마이크
                ↓
WASAPI 캡처 → 16 kHz mono PCM 변환
                ↓
음성 구간 판단 → Qwen3-ASR / Whisper / 선택적 Gemini Live
                ↓
문장 확정·경계 처리 → 모델별 번역 입력 구성
                ↓
로컬 llama.cpp 번역 → 출력 검사
                ↓
최근 자막 / 두 구절 오버레이 / 최대 100개 로컬 기록
```

소스와 실행 환경의 주요 경로는 다음과 같습니다. `runtime`, 모델 가중치, 개인 `config`·`logs`는 공개 소스에 포함하지 않습니다.

```text
LiveSubtitle/
├─ src/LiveSubtitle.App/       C# WPF 화면·오디오 캡처·오버레이
├─ engine/                    Python 음성 인식·번역·로컬 API
│  ├─ tests/                  엔진 회귀 테스트
│  ├─ _vendor/                포함된 외부 코드·자산·원문 라이선스
│  ├─ requirements.txt        Python 의존성
│  ├─ runtime.py              모델 수명 관리·추론·번역 출력 검사
│  ├─ translation_profiles.py 모델별 번역 입력 형식
│  └─ server.py               HTTP/WebSocket 엔진 진입점
├─ models/                    예시 설정·토크나이저·다운로드 안내 (가중치 제외)
│  ├─ asr/                    음성 인식 모델별 폴더
│  └─ translation/            번역 GGUF 파일
├─ runtime/                   별도 구성 또는 실행본에 동봉되는 환경
│  ├─ python/                 Python·추론 라이브러리
│  └─ llama/                  llama-server와 관련 DLL
├─ config/                    실행 시 생성되는 개인 설정·인증 정보
├─ logs/                      실행 시 생성되는 번역 기록·엔진 로그
├─ 0_UserManual(사용자매뉴얼)/ 한국어·영어·중국어·일본어 매뉴얼
├─ build.ps1                  Windows 앱 빌드
├─ API.txt                    로컬 엔진 API 설명
├─ SOURCE_RELEASE.txt         개발 환경·소스 배포 안내
├─ THIRD_PARTY_NOTICES.txt     외부 구성요소 고지
├─ LICENSE                    프로젝트 라이선스
└─ README.md
```

앱과 엔진은 로컬 HTTP/WebSocket API로 분리되어 있습니다. 엔진은 `127.0.0.1`에 바인딩하고 인증 토큰으로 요청을 확인합니다. 향후 브라우저 확장 등의 클라이언트를 연결할 수 있는 구조이며, 현재 제공되는 사용 화면은 Windows 앱입니다. 인터페이스는 [API 문서](API.txt)를 참고하세요.

## 사용된 기술 및 참고자료

### 실제 사용 기술

| 영역 | 구현과 역할 | 참고자료 |
| --- | --- | --- |
| Windows UI | C# · .NET 10 · WPF. Windows Forms 알림 영역을 이용한 트레이 동작 | [WPF](https://learn.microsoft.com/dotnet/desktop/wpf/overview/) |
| 오디오 입력 | WASAPI 프로세스 루프백·시스템 루프백·마이크 캡처. FIR 필터를 포함한 16 kHz mono PCM16 변환과 100 ms 전송 | [Microsoft 프로세스 루프백 예제](https://learn.microsoft.com/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/) · [앱 캡처 코드](src/LiveSubtitle.App/Services/AudioCapture.cs) · [PCM 변환 코드](src/LiveSubtitle.App/Services/StreamingPcm16Converter.cs) |
| 로컬 API | Python · FastAPI · WebSocket, 모델 준비·수명 관리와 오디오 수신·번역 작업 분리 | [FastAPI](https://fastapi.tiangolo.com/) · [엔진 구성](engine/README.md) |
| Qwen 음성 인식 | Qwen3-ASR-1.7B를 Transformers·PyTorch로 로컬 추론 | [Qwen 공식 모델](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf) |
| Whisper 음성 인식 | faster-whisper·CTranslate2로 Whisper large-v3-turbo 로컬 추론 | [Whisper](https://github.com/openai/whisper) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1) · [CTranslate2](https://github.com/OpenNMT/CTranslate2) |
| 음성 구간·확정 | RMS와 Silero VAD를 이용한 구간 판단, 모드별 문장 확정, 경계 중복 처리, 첫 추론 준비 | [Silero VAD](https://github.com/snakers4/silero-vad) · [기본 Qwen 구간 처리](engine/fast_qwen.py) · [문장 처리](engine/streaming_sentences.py) |
| 로컬 번역 | llama.cpp CUDA 서버와 GGUF. 모델별 전용 프롬프트, 지원 언어·빈 출력·잘림·요청 JSON 누출 검사 | [llama.cpp 사용 버전](https://github.com/ggml-org/llama.cpp/releases/tag/b11378) · [번역 프로필](engine/translation_profiles.py) |
| 번역 모델 | HY-MT2, MiLMMT, TranslateGemma, JA-KO-VN의 전용 입력 형식 지원. 언어 범위와 문맥 사용 방식은 모델별로 다름 | [HY-MT2](https://huggingface.co/tencent/Hy-MT2-7B) · [MiLMMT](https://huggingface.co/xiaomi-research/MiLMMT-46-12B-v1.0) · [TranslateGemma](https://huggingface.co/google/translategemma-12b-it) · [JA-KO-VN](https://huggingface.co/hell0ks/ja-ko-vn-12b-v2-gguf) |
| 선택적 클라우드 인식 | Gemini Live로 음성을 전사한 뒤 로컬 모델로 번역 | [Gemini Live API](https://ai.google.dev/gemini-api/docs/live) |

번역 결과의 문자·형식 검사는 의미 정확도 검증이 아닙니다. 음성 인식 오류, 소음·반주, 끊긴 문장, 인명 및 모델의 번역 성향에 따른 오류가 남을 수 있습니다.

### 참고한 공개 구현

- **[WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit/tree/363e4f6d029694d9c81ae548beddd9d3c88a3637):** 선택 기능인 Whisper AlignAtt의 decoder·CTranslate2 연결·tokenizer/timing 일부를 포함했습니다. WhisperLiveKit 서버와 UI 전체를 사용하는 것은 아닙니다. 포함 범위·수정 기록은 [`UPSTREAM.json`](engine/_vendor/whisperlivekit/UPSTREAM.json)에 있습니다.
- **[SimulStreaming](https://github.com/ufal/SimulStreaming) · [SimulWhisper](https://github.com/backspacetg/simul_whisper):** 위 AlignAtt 계열 코드의 선행 구현과 스트리밍 알고리즘 출처입니다.
- **[Whisper-Streaming](https://github.com/ufal/whisper_streaming):** 반복 인식 결과의 공통 부분을 확정하는 LocalAgreement 정책을 참고했습니다. 동일 프로그램이나 모든 옵션을 그대로 사용하는 것은 아닙니다.
- **[LiveCaptions-Translator](https://github.com/SakiRinn/LiveCaptions-Translator/tree/a6fee12757b15edbeef7c60f8895e0be694801e1):** 자막 확정·번역·표시를 나누는 처리 구조를 참고했습니다. LiveSubtitle은 해당 프로젝트의 Windows LiveCaptions 음성 인식 경로를 사용하지 않습니다.

## 테스트 결과

[추가 개선 시험 기록](docs/tests/2026-10-06-context-boundary.md): 같은 Qwen 모델에서 자동 문맥·음성 구간 경계 조정을 비교했습니다. 두 후보 모두 채택 조건을 충족하지 못해 앱 소스와 기본값은 유지했습니다. 아래의 기존 모델 비교와 구분되는 후속 시험입니다.

**평가일: 2026-10-05 · NVIDIA GeForce RTX 4080 16GB · 중국어 드라마 음성→한국어 자막.**

제공받은 테스트 영상 안내 링크입니다. 실제 평가 구간과 결과는 아래의 로컬 입력 파일 기준 설명을 따릅니다.

- **테스트 영상:** [YouTube 영상](https://youtu.be/ndhgOQNXx9M)
- **샘플1:** [YouTube 영상](https://youtu.be/Q9-UW8XsW18)
- **샘플2:** [YouTube 영상](https://youtu.be/2VZ6pnNiHjc)
- **샘플3:** [YouTube 영상](https://youtu.be/njwKA9sdcPI)

Qwen3-ASR-1.7B, Whisper large-v3-turbo, Gemini Live와 두 번역 모델을 조합하여 **4개 입력 영상 × 3개 음성 인식 모델 × 2개 번역 모델 × 각 3회 = 총 72회** 실행했습니다. 72회 모두 비교 가능한 상태로 완료했으며, 실행 제외는 없었습니다. 실행 완료가 모든 문장의 번역 성공을 뜻하지는 않습니다.

- **테스트영상:** 0~99.82초, 참고 대사 38개. 화면과 대조한 SRT를 사용하며 기존 평가 규칙에 따라 첫 33 ms 대사 하나는 제외했습니다.
- **샘플1·2·3:** 같은 에피소드를 세 파일로 나눈 자료의 **각 파일 처음 180초**, 참고 대사 68·67·49개. 제공 SRT 전체의 문구와 시각을 독립 검수한 정답 자료는 아닙니다.
- 전체 분모는 **222개 참고 대사, 정규화 원문 1,533자**입니다. 반복 실행을 서로 다른 대사로 세지 않았습니다.
- 동일한 16 kHz mono PCM을 실제 시간에 맞춰 공급했습니다. 입력 언어는 중국어 지정, ASR 힌트는 비움, 기본 확정 방식, Qwen 문장 경계 재확인은 꺼짐으로 시험했습니다.
- Qwen은 BF16·Transformers, Whisper는 faster-whisper의 `int8_float16`, Gemini는 당시 요청 ID `models/gemini-3.5-transcribe-live`를 사용했습니다. Gemini 서버 내부 버전·정밀도는 확인하지 못했습니다.

### 원문 오류율 — 참고 자막 기준, 낮을수록 좋음

참고 원문과 인식 원문의 **문자 오류율(CER)**입니다. 간번체·공백·문장부호·자막 서식 등을 정규화한 뒤 문자 대체·삭제·삽입을 비교했습니다. 같은 ASR의 원문 결과가 두 번역 모델×각 3회에서 동일하여 한 표로 합쳤습니다.

| 테스트 영상 | 참고 대사 수 | Qwen3-ASR-1.7B | Whisper large-v3-turbo | Gemini Live |
| --- | ---: | ---: | ---: | ---: |
| 테스트영상 | 38 | 15.88% | 19.49% | 24.19% |
| 샘플1 | 68 | 7.06% | 13.51% | 12.10% |
| 샘플2 | 67 | 9.32% | 11.86% | 12.50% |
| 샘플3 | 49 | 10.07% | 15.97% | 19.10% |
| **합계** | **222** | **9.92%** | **14.55%** | **15.72%** |

합계는 영상별 백분율의 단순 평균이 아닌 **전체 문자 편집 오류 수 ÷ 전체 참고 문자 수**입니다. 각각 152/1,533, 223/1,533, 241/1,533입니다. 참고 SRT의 누락·표기 차이도 영향을 주므로 공인 음성 인식 정확도로 해석하지 않습니다.

### 내용 적합도 — HY-MT2 번역, 높을수록 좋음

번역 모델: **HY-MT2-7B · Q6_K**. 점수는 100점 환산입니다.

| 테스트 영상 | 참고 대사 수 | Qwen3-ASR-1.7B | Whisper large-v3-turbo | Gemini Live |
| --- | ---: | ---: | ---: | ---: |
| 테스트영상 | 38 | 84.6 | 70.6 | 64.5 |
| 샘플1 | 68 | 88.7 | 75.2 | 83.1 |
| 샘플2 | 67 | 81.6 | 65.2 | 69.4 |
| 샘플3 | 49 | 89.8 | 69.4 | 69.0 |
| **합계** | **222** | **86.1** | **70.1** | **72.7** |

### 내용 적합도 — MiLMMT 번역, 높을수록 좋음

번역 모델: **MiLMMT-46-12B-v1.0 · i1-Q4_K_M**. 점수는 100점 환산입니다.

| 테스트 영상 | 참고 대사 수 | Qwen3-ASR-1.7B | Whisper large-v3-turbo | Gemini Live |
| --- | ---: | ---: | ---: | ---: |
| 테스트영상 | 38 | 82.9 | 67.1 | 64.5 |
| 샘플1 | 68 | 89.0 | 69.9 | 82.4 |
| 샘플2 | 67 | 77.6 | 62.7 | 66.7 |
| 샘플3 | 49 | 90.8 | 68.4 | 67.3 |
| **합계** | **222** | **84.9** | **66.9** | **71.2** |

#### 내용 적합도의 평가 방법과 범위

AI가 각 참고 대사의 중국어 의미와 최종 한국어 결과를 직접 대조하여 **핵심 의미 보존 2점 / 부분 보존 1점 / 핵심 변경·미전달 0점**을 부여했습니다. 문자 오류율을 번역 점수로 환산한 것이 아닙니다. 문장이 나뉘어도 인접 최종 자막에서 뜻이 이어지면 인정하고, 말투나 자연스러운 의역·동일 인명의 음역 차이만으로 감점하지 않았습니다.

각 영상 점수는 3회 평가의 평균입니다. 합계는 **222개 대사 × 3회 × 2점 = 1,332점**을 분모로 한 획득 점수의 100점 환산이며, 영상별 점수의 단순 평균이 아닙니다.

이 비교는 **전사 오류까지 포함한 앱 전체 조합 평가**입니다. HY-MT2에는 이전 확정 원문 최대 3개/900자와 기존 용어집 5항목을 적용했고, MiLMMT에는 이전 문맥·용어집을 넣지 않았습니다. 모델별 권장 입력 형식과 샘플러도 달라, 동일 조건에서 번역 모델만 바꾼 독립 성능 순위로 해석할 수 없습니다.

원어민 복수 검수나 공인 벤치마크가 아닌 AI 의미 평가이며 참고 SRT와 평가자의 해석에 영향을 받습니다. 점수는 지연·유창성·참고 자막 밖의 추가 출력 전체를 평가하지 않습니다. 영어·일본어·한국어 입력, 노래·게임·다른 장르, AlignAtt 및 JA-KO-VN 성능으로 일반화하지 않습니다. 작은 점수 차이를 확정적인 우열로 보지 마세요.

## 라이선스

LiveSubtitle에서 직접 작성한 코드와 문서는 [MIT License](LICENSE)로 공개합니다. 라이선스 전문과 저작권 표기는 저장소의 `LICENSE`를 확인하세요.

포함된 외부 코드와 별도로 사용하는 라이브러리·모델에는 각자의 라이선스가 적용됩니다. 프로젝트의 MIT 라이선스가 이를 대체하지 않습니다. 출처·원문 고지·적용 범위는 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)와 [`engine/_vendor`](engine/_vendor/)에 보존합니다.
