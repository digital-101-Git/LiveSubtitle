# LiveSubtitle 구현·실험·변경 이력

작성·최종 대조: **2026-10-05** · 제작자: **digital-101** · 프로젝트 자체 코드/문서: [MIT](LICENSE)

이 문서는 다음 개발자가 이미 적용한 기능을 다시 만들거나, 실패한 실험을 같은 조건으로 반복하지 않도록 유지하는 개발 기록이다. 사용법은 [0_사용법/사용법.md](0_사용법/사용법.md), API 계약은 [API.txt](API.txt), 외부 구성요소 고지는 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)를 참고한다.

## 1. 변경 전에 읽을 사항

1. **현재 구현, 실험 완료, 미검증 후보를 구분한다.** 코드가 있거나 CPU 모의 검사를 통과했다는 사실만으로 실제 GPU·전체 자막 품질까지 검증됐다고 쓰지 않는다.
2. 아래 실험 ID와 근거를 먼저 확인한다. 같은 조건의 재실행은 기본 작업으로 잡지 않는다. 회귀 의심, 소스/모델/런타임/하드웨어 변경, 새로운 실패 표본, 명시적 사용자 요청처럼 **다시 실행하는 이유**를 먼저 기록한다.
3. 미채택 실험을 다시 검토할 때는 바뀐 가설·변수·통과 기준을 명시한다. 이전 결과를 지우지 말고 새 결과와 연결한다.
4. **개선 우선순위는 정확도 1순위, 속도 2순위다(2026-10-06 사용자 재확인).** 정확도 개선은 현재보다 느려지지 않아야 한다. 같은 속도에서 더 정확해지는 변경도 채택 가치가 있으며, 정확도를 유지한 속도 개선은 그다음이다. 문장을 버리거나, 실제 반복을 지우거나, 필요한 문맥을 무조건 줄여 따라잡지 않는다. 원문 CER뿐 아니라 누락·중복·부정·숫자·인명·최종 번역 의미를 먼저 보고, 같은 조건의 로그로 인식부터 자막 발행까지 지연 비악화를 확인한다. ASR 단독 가속만으로 이 조건을 통과했다고 판단하지 않는다.
5. 사용자가 입력하는 힌트를 개선책의 전제로 삼지 않는다. 힌트 기능은 선택 사항이며 기본값은 비어 있다.
6. 지연은 로그로 분석한다. 사용자가 제거하도록 한 **화면 측정·렌더링 시각 수집 기능을 다시 넣지 않는다.** 표시 예약은 가짜 시계/Dispatcher 검사로 검증할 수 있다.
7. 실험은 원본 가중치를 참조한다. **백업을 만들 때 큰 모델 파일을 중복 보관하지 않는다.** 모델 설치/다운로드 자체를 금지한다는 뜻은 아니다.
8. 현재 실제 작업본은 워크스페이스의 `outputs/LiveSubtitle/`이다. **E드라이브의 소스·배포본은 최종 복사 지시 전까지 그대로 둔다.** GitHub 게시도 별도 작업이다.
9. 각 절의 실제 실행 여부와 날짜를 구분한다. 기존 실험의 수치를 문서 정리 중 재측정한 것으로 해석하지 않는다. §15는 후보별 GPU 시험, §16은 로컬/Gemini Live의 기존 영상 6회 비교, §17은 후속 사용자 요청으로 새로 실행한 4개 입력·3 ASR·2 번역 모델·3회 반복의 72회 비교다. §16과 §17의 실행을 합산하지 않는다.

상태 표기:

| 상태 | 의미 |
|---|---|
| 적용 | 현재 생산 소스에 들어 있음. 변경할 때 기존 회귀 조건을 지킬 것 |
| 선택 기능 | 구현은 있으나 현재 기본 경로가 아님 |
| 실험 후 보류 | 실제 비교 결과가 있음. 같은 조건으로 재시도할 이유가 부족함 |
| 검토만 / 미검증 | 근거나 CPU 재현은 있으나 실제 적용·효과는 아직 검증하지 않음 |
| 중단 / 미완료 | 결과 파일 일부만 있음. 성공·실패 결론을 새로 만들어내지 말 것 |

### 현재 결론 요약

| 항목 | 상태 | 유지할 판단 |
|---|---|---|
| Windows WPF 앱 + 별도 로컬 Python 엔진 | 적용 | 앱이 엔진을 자동 실행. 사용자가 두 프로그램을 따로 켤 필요 없음 |
| Qwen 빠른 확정, 신경망 종료 힌트, 무음 중 사전 인식과 결과 재사용 | 적용 | 같은 기능을 신규 최적화처럼 다시 제안하지 않음 |
| FIR 음성 변환·모델 첫 추론 준비·강제 경계의 짧은 연속 꼬리 보존 | 적용 | 각 기능의 실제 검증 범위는 아래 이력 참고 |
| Qwen 문장 경계 재확인 | 선택 기능, 기본 꺼짐 | 인식 개선 가능성과 추가 대기가 함께 존재 |
| 안정 확정 / Whisper AlignAtt | 선택 기능 | 기본 모드를 일괄 대체하지 않음 |
| 번역 HTTP 클라이언트 재사용(B1) | 적용·영상 반복 검증 | 약 0.2초의 반복 초기화 비용 제거 |
| 자막 교체 시각 예약(A3) | 적용·CPU/Dispatcher 검증 | 위치 추적 250ms 타이머와 분리. 두 줄 읽기 시간 유지 |
| Qwen `tolist()` 재사용(B2) | 실험 후 보류 | 복사는 감소했지만 실제 ASR 시간 이득이 편차 수준 |
| Qwen `torch.compile`(B3) | 실험 후 기본 적용 보류 | 반복 ASR은 빨라지나 캐시가 있어도 48초·8초 정지 발생 |
| Nemotron으로 기본 ASR 교체 | 실험 후 보류 | 동일 중국어 PCM에서 전사 오류가 더 많았음 |
| CR1·CR2·CR3 / Whisper FP16 | 실험 후 미채택 | §12의 실제 결과를 우선. 기존 문맥·분할·Silero·INT8_FLOAT16 유지 |
| Fun-ASR-Nano / FireRedASR2로 ASR 교체 | 공개 자료 검토 후 시험 보류 | §13 참고. 현재 Qwen 유지, 실제 앱 속도·품질 우위는 미확인 |
| CR3 분절·후단 필터 충돌 추가 검증 | 검토 후 보류 | 미채택 실험판의 문제. 현재 기본판의 해당 구간은 통과했으며 필수 수정으로 취급하지 않음 |
| 현재 실행 기술 심층 자료 대조 | 조사 후 후보별 실제 시험 완료 | §14 조사와 §15 후속 시험을 구분. 큰 구조 변경의 가속 근거를 찾지 못함 |
| D1 HY 샘플링 순서 | 실제 번역 438회 후 미채택 | 대응 219쌍 중204쌍 동일, 확인된 의미 개선 없음. 번역 p50 180.13→181.81ms |
| D2 Qwen 오류 문구 CUDA 스칼라 변환 | 실제 GPU·ASR 시험 후 미채택 | helper 약0.0206ms 절감은 있으나 실제 ASR 시간 개선 없음 |
| D3 Whisper 시각 토큰 생략 + 단어 정렬 | 실제 ASR 36회 후 미채택 | 꼬리 재인식·불필요한 문구 증가, 반복 ASR p50 149.38→285.80ms |
| 현재 로컬 Qwen / Gemini Live | 동일 영상 각3회 비교 완료 | §16. 이 영상에서는 로컬 CER 15.88% / Gemini 24.19%, 공통12대사 확정 자막 전달 지연 중앙값 1.20 / 1.52초. 전체 언어·영상으로 일반화하지 않음 |
| 4개 입력 × 3 ASR × 2 번역 모델 × 3회 | 새 72회 비교 완료 | §17 및 전사모델비교.md. Qwen의 참고 CER가 4개 입력 모두 가장 낮음. 공통 대사 지연은 3개 입력에서 Qwen, sample2에서 Whisper가 짧음. 현재 Qwen+HY 유지가 타당하며 MiLMMT의 일괄 우위는 확인하지 못함 |
| Gemini 확정 대사의 다음 잠정 전사 재포함 | 실제 이벤트에서 관측, 미수정 | §16.4. 3회 중2회에서 잠정 반복 후 교정·철회. 최종 CER와 별도로 관리할 문제 |

## 2. 실제 구조와 소스 위치

```text
Chrome / Edge / 사용자가 선택한 시스템 소리
  → Windows WASAPI 루프백 캡처(브라우저 프로세스 / 시스템 출력)
  → mono 16 kHz PCM16LE 변환 및 100ms 묶음
  → 인증된 localhost WebSocket
  → Python 엔진: 음성 수집·분절·ASR·확정/수정 관리
      로컬: Qwen3-ASR 또는 Whisper
      선택: Gemini Live 전사
  → localhost llama.cpp + 선택한 GGUF로 한국어 번역
  → session_id / segment_id / revision을 가진 caption 이벤트
  → WPF 최근 자막 + 두 줄 오버레이
  → 최근 100건 JSON 기록(과거 UI 복원은 하지 않음)
```

| 영역 | 주요 파일 | 역할 |
|---|---|---|
| 주 화면·모델·언어 선택 | [MainWindow.xaml](src/LiveSubtitle.App/MainWindow.xaml), [MainWindow.xaml.cs](src/LiveSubtitle.App/MainWindow.xaml.cs) | 준비/시작/중지, 상태, 최근 원문·번역 복사 |
| 창/프로세스 선택·캡처 | [WindowTargets.cs](src/LiveSubtitle.App/Services/WindowTargets.cs), [AudioCapture.cs](src/LiveSubtitle.App/Services/AudioCapture.cs) | Chrome/Edge와 팝업 식별, WASAPI 캡처 |
| 음성 변환 | [StreamingPcm16Converter.cs](src/LiveSubtitle.App/Services/StreamingPcm16Converter.cs) | 연속 FIR 리샘플링·채널 혼합·PCM 변환 |
| 앱↔엔진 | [EngineClient.cs](src/LiveSubtitle.App/Services/EngineClient.cs), [SubtitleSession.cs](src/LiveSubtitle.App/Services/SubtitleSession.cs) | 엔진 소유권, HTTP/WS, 전송 큐·세션 수명 |
| 로컬 서버 | [server.py](engine/server.py), [http_guard.py](engine/http_guard.py), [settings.py](engine/settings.py), [models.py](engine/models.py) | 인증·설정·모델 검색/가져오기·API |
| 모델 수명·번역 | [runtime.py](engine/runtime.py), [translation_profiles.py](engine/translation_profiles.py), [glossary.py](engine/glossary.py) | GPU ASR, 소유 llama 서버, 모델별 프롬프트·검사 |
| 전체 세션·번역 큐 | [sessions.py](engine/sessions.py), [local_captions.py](engine/local_captions.py) | 전사/잠정/확정/교정, 순서·실패 복구 |
| Qwen 빠른 경로 | [fast_qwen.py](engine/fast_qwen.py), [fast_qwen_session.py](engine/fast_qwen_session.py), [asr_qwen.py](engine/asr_qwen.py) | 독립 수집/추론, 종료·사전 인식·경계·GPU 실행 |
| 안정/실험 ASR | [stable_sessions.py](engine/stable_sessions.py), [qwen_streaming.py](engine/qwen_streaming.py), [local_streaming.py](engine/local_streaming.py), [asr_alignatt.py](engine/asr_alignatt.py) | LocalAgreement, 단어/어절 확정, AlignAtt |
| 준비·힌트·경계 | [asr_warmup.py](engine/asr_warmup.py), [asr_hints.py](engine/asr_hints.py), [qwen_boundary.py](engine/qwen_boundary.py), [qwen_revisions.py](engine/qwen_revisions.py) | 첫 추론 준비, 선택 힌트, 추가 확인/교정 |
| 오버레이 | [OverlayWindow.cs](src/LiveSubtitle.App/OverlayWindow.cs), [CaptionDisplayQueue.cs](src/LiveSubtitle.App/Services/CaptionDisplayQueue.cs), [OverlayAlignment.cs](src/LiveSubtitle.App/Services/OverlayAlignment.cs), [UiSettings.cs](src/LiveSubtitle.App/Services/UiSettings.cs) | 두 줄·예약·DPI/모니터·위치 저장 |
| 기록 | [history.py](engine/history.py) | 최근 100건 회전·수정·원자적 저장 |

### 연결·소유권·중지 조건

- C#/.NET 10 WPF가 `runtime/python/python.exe`로 FastAPI/Uvicorn 엔진을 자동 실행한다. 이미 실행된 외부 엔진에 연결했을 때는 그 엔진을 소유한 것으로 취급하지 않는다.
- 기본 주소는 `127.0.0.1:17865`. `/health`를 제외한 API는 토큰 인증을 사용한다. WebSocket 첫 메시지 인증과 단일 활성 세션 제한이 있다.
- 일반 웹사이트 Origin을 거부한다. Chromium 확장 Origin 허용도 토큰 인증을 생략하는 기능이 아니다. 네이티브 파일 가져오기 API를 브라우저에 그대로 열지 않는다.
- 번역용 llama 서버는 엔진이 임의의 로컬 포트와 별도 키로 시작·관리한다. C#↔엔진 HTTP와 Python↔llama HTTP는 다른 연결이다.
- `중지`는 캡처/세션의 취소다. 남은 버퍼를 강제로 최종 자막으로 내보내는 명령이 아니다. 모델은 빠른 재시작을 위해 유지한다. `release`/완전 종료는 소유 모델과 서버를 해제한다.
- 네이티브 ASR가 취소 즉시 끝난다고 가정하지 않는다. 작업 완료·잠금·세션 ID 검증으로 늦은 결과와 모델 해제 경쟁을 막는다.
- 창 닫기/최소화는 트레이로 이동한다. 완전 종료와 구분한다.
- 브라우저 확장 자체는 아직 구현되지 않았다. 현재 앱의 로컬 API를 향후 확장에서 재사용할 수 있도록 분리된 구조다.

### 캡처·버퍼

- 브라우저 프로세스 트리의 소리를 캡처하므로 같은 프로세스의 다른 탭/창 소리가 포함될 수 있다. 현재 구현을 탭 단위 캡처라고 설명하지 않는다.
- 프로세스 캡처에는 Windows build 20348 이상이 필요하다. 해당 캡처 실패를 시스템 전체 캡처로 자동 전환하지 않는다.
- 시스템 전체 소리는 사용자가 그 소스를 선택했을 때만 캡처한다. 낮은 볼륨과 음소거를 자동 증폭으로 해결하는 기능은 아니다.
- 스트림은 raw PCM16LE, mono, 16 kHz. 기본 100ms는 3,200바이트다.
- 다운샘플링에는 Blackman-windowed sinc polyphase FIR를 적용했다. 48 kHz 입력의 필터 미래 샘플 대기는 약 2ms이며, 100ms 패킷 조립 대기와 구분한다. 16 kHz 입력은 해당 리샘플링을 우회한다.
- 앱 전송 큐는 60패킷, 엔진 `AudioQueue`는 120개 메시지 및 192,000바이트(6초)로 제한한다. 청크를 20ms로 줄이고 개수 제한을 유지하면 앱은 1.2초, 엔진은 바이트 한도에 도달하기 전 2.4초에서 차게 된다. 패킷 축소 실험에서는 양쪽 개수 제한과 시간/바이트 용량을 함께 검토해야 한다.
- 지연을 감추려고 오래된 음성/서로 다른 확정문을 임의로 버리지 않는다. 과부하 제한·오류 정책과 선택적인 잠정 결과 억제를 구분한다.

## 3. 음성 인식과 확정 방식

기본 확정 방식 키는 `legacy`(빠른 표시)다. **Qwen의 legacy와 Whisper의 legacy는 같은 알고리즘이 아니다.** 저장된 모델 선택, 코드의 기본 fallback 문자열, 아래 시험 조합도 서로 다르다.

### Qwen3-ASR 빠른 표시 — 현재 주요 검증 경로

- Transformers 네이티브 Qwen3-ASR, CUDA BF16, 로컬 `safetensors`, `trust_remote_code=False`, greedy 생성(`do_sample=False`)을 사용한다. SDPA 경로는 이미 사용 중이다.
- 오디오 수집과 추론을 분리한다. 열린 음성 구간의 원본 PCM을 유지하고 신경망 VAD의 음성 아님 판정만으로 내용을 잘라 버리지 않는다.
- 20ms RMS 프레임, 시작 문턱값 0.002, 시작 전 최대 400ms 보존, 기본 종료 약 500ms, 최대 구간 4초. 강제 경계에는 마지막 200ms를 겹친다.
- RMS 무음과 종료 후보가 약 200ms 함께 이어지면 먼저 인식을 계산할 수 있다. 최종 종료까지 음성이 재개되지 않으면 재사용하고, 새 소리가 들어오면 오래된 추측을 무효화한다. 배경음이 이어지고 신경망만 무음으로 판단할 때는 이 사전 인식을 하지 않는다.
- 사전 인식은 **계산 준비**이며 Qwen 기본 경로에 잠정 한국어 자막을 추가하는 기능이 아니다. 번역은 확정 원문으로 진행한다.
- 수집된 전체 파형에 Silero 비음성 게이트도 적용한다. 해당 라이브러리의 `min_speech_duration_ms=0` 및 `min_silence_duration_ms=2000`을 각각 숨겨진 250ms 발화 제한/실시간 2초 대기로 오해하지 않는다.
- 온라인 종료 힌트는 스트림별 Silero 상태를 유지한다(512샘플 창, 64샘플 문맥, 진입 0.5/이탈 0.35). 전체 파형 게이트와 다른 역할이므로 ‘VAD가 어디에서도 음성을 거르지 않는다’고 쓰지 않는다.
- 강제 경계의 중복 정리는 실제 겹친 구간에 한정한다. 문자 중복만으로 실제 반복 발화와 겹침을 완전히 구분하지는 못한다.
- 4초 강제 경계뿐 아니라 신경망 종료 힌트로 연속 발화가 나뉜 뒤의 짧은 **연속 꼬리** 보존도 이미 수정했다. 새 독립 발화의 최소 길이를 완화한 것이 아니다.
- 빠른 컨트롤러 내부 대기는 10초/최대 8개 창으로 제한한다. 서버 수신 6초, 안정 경로 30초와 서로 다른 층의 제한이다.

### 선택 사항·다른 인식 경로

| 경로 | 구현 | 주의 |
|---|---|---|
| Qwen 문장 경계 재확인 | 강제 분할에 뒤 음성을 더해 한 번 재확인. 기본 `false` | 최대 약 1초 입력 대기와 추가 추론이 생길 수 있음. 총 지연 상한 1초라는 뜻 아님 |
| Qwen 안정 확정 | 연속 인식 간 안정된 접두부를 확정하는 LocalAgreement 계열 | 원문 보존/확정성을 위해 더 늦을 수 있음. 빠른 모드의 무비용 대체재가 아님 |
| Whisper 빠른 경로 | faster-whisper/CTranslate2, 단어 타임스탬프·로컬 스트리밍 확정·잠정 자막 | 해당 legacy 루프의 음성 공급/추론 분리 후보 A1은 아직 미적용 |
| Whisper 안정 경로 | 독립 공급 driver와 확정된 구절 관리 | 기본 경로와 동일한 표시·지연이라고 가정하지 않음 |
| Whisper AlignAtt | `_vendor`의 WhisperLiveKit/SimulStreaming 계열 attention 확정. 실험 옵션 | Whisper 전용, 별도 `.pt` 체크포인트 필요. 짧은 확정 꼬리 대기 후보 A7 미해결 |
| Gemini Live | 선택한 모델의 실시간 전사 → 동일한 로컬 번역 | 선택 모드에서만 방송 음성이 Google로 전송됨. 전사/번역 공급자를 혼동하지 않음 |

입력 언어 `auto/en/zh/ja`를 지원한다. 명시한 언어는 전사와 번역에 사용한다. 미지정은 자동 감지이며, 필드 생략과 명시적인 비우기는 API 계약대로 구분한다.

힌트는 Qwen 선택 기능이다. 원어 인명 등을 줄마다 하나씩 입력하며 최대 32개·항목 48자·전체 512자다. 강제 정답 삽입이 아니며, 성능 비교에서는 기본적으로 비운다.

안정 Qwen은 첫 1.2초 이후 약 1초마다 같은 발화 시작점부터 늘어난 PCM을 재인식한다. 느릴 때는 간격을 최대 2초까지 조정하며 최대 발화 12초/대기 버퍼 30초를 사용한다. 두 관측의 안정된 접두부를 확정하고 마지막 결과가 바뀌면 같은 ID를 교정한다. 이를 Qwen 모델 내부의 음성 KV 캐시를 다음 구간에 단순 누적하는 방식이라고 설명하지 않는다.

기본 Whisper는 CUDA `int8_float16`, beam 5, temperature 0, `condition_on_previous_text=False`, 내부 VAD와 단어 타임스탬프를 사용한다. 타임스탬프는 경계 회수·중복 방지의 근거이므로 속도를 위해 무조건 끄지 않는다. 현재 설치 버전에서 짧은 `chunk_length`도 encoder 입력이 3000 frame으로 패딩되는 경로가 있어, 그 값만 줄였다고 계산량이 비례해 줄었다고 추정하지 않는다.

### 준비 추론과 GPU

- 모델 준비 단계에서 1초 합성 오디오로 첫 추론을 준비한다. 방송 소리나 SRT를 예열 입력으로 쓰지 않고 예열 결과를 번역·자막·기록으로 보내지 않는다.
- 같은 로드된 모델의 완료된 준비는 재사용한다. 취소 중 실제 네이티브 작업이 끝나기 전에 모델을 해제하지 않는다.
- Qwen 준비는 영어 지정·최대 신규 2토큰 후 CUDA 완료를 기다린다. Whisper는 지연 실행 iterator를 실제로 소비한다. 모든 길이·단어 정렬·언어 경로의 최초 비용까지 제거됐다는 보장은 아니다.
- 모델 메모리는 대기 중에도 유지될 수 있다. GPU 계산 부하는 ASR/번역 작업 때 올라가므로 VRAM 사용과 지속적인 GPU 연산율은 같은 지표가 아니다.
- Qwen `torch.compile`은 생산 경로에 **켜져 있지 않다**. 선택 실험의 2배 가속 수치를 현재 실행본 성능으로 설명하지 않는다.

## 4. 번역·교정·기록

### 모델 검색과 실행

- Ollama 서버를 요구하지 않는다. 앱이 동봉한 llama.cpp를 실행한다.
- 목록은 `models/asr/`, `models/translation/`의 실제 모델을 검사해 구성한다. 지원 형식 검증, 저장 경로, 기본 fallback, 모델별 프롬프트 선택 규칙은 코드에 있다. **목록 전체가 하드코딩됐다는 설명도, 어떤 파일이든 무조건 지원한다는 설명도 틀리다.**
- `번역 모델 추가(GGUF)`는 번역 폴더로 복사하는 기능이다. ASR를 GGUF 가져오기로 설치하는 기능이 아니다.
- ASR는 인식기 형식에 맞는 설정/토크나이저/가중치가 필요하다. 배포본이 부속 파일을 제공하더라도 `safetensors` 하나가 임의의 빈 폴더에서 독립 실행되는 것은 아니다. Whisper 기본 가중치는 CTranslate2 `model.bin`이며 Qwen의 `model.safetensors`와 다르다.
- 번역 모델 파일명은 프로필 선택에 사용된다. 임의로 이름을 바꾸면 전용 프롬프트/EOS 처리가 달라질 수 있다.

| 번역 프로필 | 현재 계약 | 반복하면 안 되는 변경 |
|---|---|---|
| HY-MT2 | `/v1/chat/completions`, 전용 user 지시. temperature 0.7 / top_p 0.6 / top_k 20 / repeat_penalty 1.05 / min_p 0, 출력 320토큰 | 일반 Qwen system/JSON prompt로 덮어쓰거나 근거 없이 greedy로 변경하지 않음 |
| MiLMMT | 공식 `Translate this from … to Korean:` raw `/completion`, greedy, 과거 문맥/사고 플래그 없음, prefill microbatch 128 | 일반 채팅 템플릿을 raw 번역 입력에 섞지 않음. 결정적 생성이므로 동일 요청의 품질 재시도를 하지 않음 |
| TranslateGemma | 전용 Gemma 번역 turn의 raw `/completion`, 현재 문장만. temperature 0, 품질 재시도 0.2 | 일반 chat/JSON이나 HY 용어집을 그대로 섞지 않음 |
| 일반 Qwen/chat | 한국어 지시, `enable_thinking=False`, 한 번의 독립된 품질 재시도 | 모든 모델에 동일한 사고 모드 옵션이 있다고 설명하지 않음 |

- HY의 정확한 `HY-MT2-7B-Q6_K.gguf` 파일에는 EOS 메타데이터 보정 `tokenizer.ggml.eos_token_id=int:127960`을 서버 시작 시 적용한다. 해당 고정 파일의 `$`(ID 3) 메타데이터 문제에 대응한 것으로, 원본 GGUF 바이트를 고치거나 모든 GGUF에 일괄 적용하지 않는다.
- HY 문맥은 성공한 확정 원문의 최근 최대 3문장·합계 900자다. 너무 긴 문장은 중간 절단 대신 제외한다. 현재 문장만 번역하도록 한다.
- HY 선택 용어집은 `config/translation-glossary.json`. 현재 원문에 정확히 일치하는 용어만 제한적으로 넣는다. 퍼지 ASR 교정/자동 인물명 정답집이 아니다. 파일 오류는 경고로 처리한다.
- 원문 직전의 번역 대상 제목/장식 라벨을 모델이 답에 복사해 한국어 검사를 실패한 이력이 있다. 프롬프트를 꾸미기 전에 기존 probe를 확인한다.
- `engine/README.md`의 과거 “current source is labeled separately” 설명을 그대로 재현하지 않는다. 현재 HY 코드와 검증은 원문 앞 제목을 제거한 상태다.
- TranslateGemma/MiLMMT의 script 기반 입력 언어 fallback에는 한자만 있는 일본어/중국어의 모호함이 있다. 명시 언어 선택이 우선이다.

### 큐·실패·수정

- 한국어 검사는 문자·형식 검사다. 올바른 의미나 자연스러운 번역을 보증하는 채점기가 아니다.
- 스트리밍에서 문장별 `translation_language/empty/truncated` 실패는 원문을 유지한 실패 자막과 warning으로 기록하고 다음 문장을 계속한다. 모델/통신 등 치명 오류는 별도로 중단한다. 실패를 무조건 세션 종료로 되돌리지 않는다.
- `session_id + segment_id + revision`으로 같은 자막의 교정을 처리한다. 늦게 끝난 예전 번역은 새 원문을 덮어쓰지 못한다.
- 같은 원문의 최종 확정은 기존 성공 번역을 재사용한다. 서로 다른 확정 문장은 각각 보존하고 FIFO로 처리한다.
- 번역 작업 큐는 6칸이며 10초를 넘는 큐 진입 대기는 명시적 과부하로 처리한다. 오버레이 60개 대기 큐와 다른 큐다.
- Whisper/Gemini 잠정 문장은 안정성 조건과 제한된 빈도로 번역한다. 확정문 대기·과부하 시 선택적인 잠정 요청을 억제하는 분기는 현재 Whisper 로컬 경로에만 적용된다. 원시 transcript를 한국어 오버레이에 직접 넣지 않는다.
- 철회된 잠정 ID는 `caption_remove`로 제거한다. 실제로 반복한 다른 발화를 같은 문자열이라는 이유만으로 없애지 않는다.
- Runtime의 품질 재시도 도중 같은 ID의 원문이 교체돼도 오래된 내부 재시도가 한 번 더 실행될 수 있는 A2는 아직 남아 있다. 결과 표시 방지와 불필요한 계산 방지는 별개의 문제다.

### B1: 이미 적용한 HTTP 재사용

- Python Runtime이 HTTPX AsyncClient 하나를 모델 준비에서 만들고 같은 이벤트 루프에서 재사용한다.
- health 요청 2초 / 번역 요청 45초, 요청 시점 URL·키, 모델 교체·모델 준비 실패/취소·종료 시 안전 정리를 유지한다. 개별 번역의 실패/취소 때는 클라이언트를 닫지 않고 다음 요청에서 재사용한다.
- TLS 검증을 끈 변경이 아니다. 번역 프롬프트·샘플링·최대 출력·재시도 횟수도 그대로다.
- 과거의 ‘localhost HTTP는 작아서 효과가 없을 것’이라는 추정은 실측으로 뒤집혔다. 생성 때 약 195ms의 비용이 있었고 이미 제거했다.

### 최근 기록·비밀 정보

- `logs/caption-history.json`은 **최신 100건 회전 기록**이다. 원문·한국어·시각·상태를 남기고 같은 ID 교정은 갱신하며 철회된 잠정 기록은 제거한다.
- 재실행 후 저장된 과거 자막을 UI/스트림에 다시 불러오지 않는다. 최근 자막 텍스트는 읽기 전용으로 선택·복사할 수 있다.
- 기록 I/O는 자막 전달과 분리한다. 원자적 교체의 Windows 공유 충돌은 10/30/60ms 재시도하며 영구 오류는 경고로 알린다.
- API 키 저장은 선택한 경우 CurrentUser DPAPI를 사용한다. API에 실제 키를 반환하거나 예외 문자열/토큰/키를 공개 문서·테스트 결과에 넣지 않는다.

## 5. 오버레이와 유지해야 할 사용자 동작

- Windows 투명 최상위 창이다. 기본 표시 내용은 한국어이며 원문은 최근 자막에서 확인한다. 저장된 사용자 선택의 원문 병기 옵션과 구분한다.
- 최근 두 구절을 `1/2 → 2/3 → 3/4`로 표시한다. 새 구절은 아랫줄, 같은 ID의 교정은 그 줄 갱신이다. 긴 문장은 한 줄 폭에 맞춰 축소한다.
- 각 줄의 처음 등장한 읽기 시각을 유지한다. 기본 읽기 시간 650~3500ms와 제한적인 backlog 압력 정책을 임의로 줄이지 않는다. 대기 자막 60개 초과는 문장을 몰래 건너뛰는 방식으로 처리하지 않는다.
- 기본 위치는 브라우저 표시다. `브라우저에 표시`/`모니터에 표시`는 **위치와 크기 모두 기본값**으로 초기화한다. 기본 크기 1000×180 DIP, 브라우저 하단 중앙·간격 24 DIP, 모니터 가로 중앙·오버레이 하단이 전체 화면 높이 95% 지점이다.
- 편집 모드에서 드래그/크기 조절하고 놓으면 즉시 저장한다. 자유 배치는 이후 자막과 앱 재실행에서도 유지한다. 편집이 꺼지면 클릭은 뒤 창으로 통과한다.
- DIP와 실제 픽셀, 서로 다른 DPI의 모니터를 구분한다. 저장한 세로 위치를 재실행 시 임의로 기본 위치로 덮어쓰지 않는다.
- 브라우저 preset이고 수동 배치가 아닐 때만, 브라우저 최소화/숨김 시 임시 모니터 배치로 전환하고 돌아오면 브라우저 기준으로 복원한다. 수동 좌표와 명시적 모니터 표시는 브라우저 상태에 따라 이동하지 않는다. 메뉴 없는 팝업에는 모니터 표시를 선택할 수 있다.
- 8초간 새 자막이 없으면 숨기고 새 자막에서 다시 표시한다. 최상위 팝업 위로 올릴 때 키보드 포커스는 빼앗지 않는다.
- 기본 모니터 배치는 `rcMonitor`의 전체 영역 기준이다. 작업표시줄을 뺀 work area의 95%가 아니다. 작은 창에 임시로 축소해도 선호 크기는 별도로 보존한다.
- **A3 적용:** 위치/숨김 확인의 250ms 타이머는 유지하고 대기 자막 교체는 `MillisecondsUntilAdvance`의 시각에 단발 예약한다. 빈 큐는 예약하지 않고 Clear/Closed에서 취소한다. 늦은 callback도 한 번에 하나만 진행한다.
- A3는 모든 자막에 붙는 250ms를 무조건 없앴다는 뜻이 아니다. 두 줄이 차 대기 중인 자막이 대상이고 Dispatcher가 바쁘면 실행은 늦을 수 있다.

## 6. 시험 기준과 수치 해석

### 기준 자료·환경

- 영상: `ndhgOQNXx9M`, [YouTube 원본](https://www.youtube.com/watch?v=ndhgOQNXx9M).
- 워크스페이스 기준 `outputs/downloads/ndhgOQNXx9M.mp4`, 이미지 대조로 정리한 같은 이름의 `.srt`가 기준이다. SRT를 ASR 입력/힌트에 주입하지 않는다.
- 최근 동일 파형: `work/asr-implementation-v6/video-new-converter-16k-mono.s16le`, PCM16LE mono 16 kHz, **99.822625초**, SHA256 `7dca4122126bcf30709b24cd8690c1d62dbdf95e03976722822f009b2f072f55`.
- 기준 SRT SHA256: `2e23a08b1e96461604f455c688881a2f4d8b958dc5c54b83201999ed80317109`.
- 최근 속도 검증 조합: **Qwen3-ASR-1.7B + HY-MT2-7B-Q6_K**, `legacy`, 중국어 지정, 경계 재확인 꺼짐, 힌트 없음. 이것을 모든 설치본의 현재 저장 설정이라고 간주하지 않는다.
- 최근 실험 환경: Windows, RTX 4080 16GB, 내장 Python 3.12.10, torch 2.11.0+cu128, Transformers 5.18.0, faster-whisper 1.2.1, CTranslate2 4.8.2, HTTPX 0.28.1. 버전 변경 시 결과의 적용 범위를 다시 판단한다.

### 지표를 혼동하지 않을 것

- 전체 자막 지연에는 수집/종료 판단, ASR, 번역 대기, 번역, 전송, 표시 읽기 대기가 포함된다. ASR-only 처리 속도를 전체 자막 속도로 바꾸어 보고하지 않는다.
- 저장 PCM 재생은 실제 브라우저·WASAPI·모니터 렌더링 시각 측정이 아니다. SRT 큐의 끝도 정밀한 실제 발화 끝 정답과는 다를 수 있다.
- 최근 재생 시험의 첫 자막은 시험용 PCM 공급 시작부터 엔진 `caption` 이벤트까지 측정한다. 실제 브라우저 재생 시작이나 오버레이 화면 표시까지의 측정이 아니다. 첫 입력 구간 4초가 큰 비중이며 GPU 준비·첫 추론·반복 추론을 분리한다.
- CER은 중국어 전사 글자 오류율이며 번역의 의미 정확도 점수가 아니다. 이 영상의 합격을 영어/일본어/모든 방송에 일반화하지 않는다.
- 최근 CER 15.8845%는 기존 정책대로 첫 0.000~0.033초 cue 1을 제외한 범위다. 포함하면 17.9577%이며 B1 전후 양쪽 모두 동일하다. 비교에 유리하게 제외 규칙을 바꾸지 않는다.
- 같은 구간 반복 51개 관측은 독립적인 영상 51개가 아니다. 모델 샘플링 때문에 번역 표현이 달라지는 것과 ASR 원문이 달라지는 것을 구분한다.
- 반복 평균/중앙값뿐 아니라 p95·최대·오류·누락/중복을 본다. 최근 백분위 계산은 `(n-1)*q` 위치 선형 보간이다.

## 7. 실험·적용 이력

아래의 `work/...`는 **워크스페이스 기준**이다. 이 파일에서 연결할 때는 `../../work/...`를 사용한다. 공개 소스만 내려받은 환경에는 로컬 실험 자료가 없을 수 있으므로 중요한 판단과 수치는 본문에 함께 남긴다.

### 7.1 B1 — 번역 HTTP 재사용: 적용 완료

근거: [통합 결과](../../work/implementation-speed-20261005/적용_및_실험_결과.txt), [고정 원문](../../work/implementation-speed-20261005/fixed-translation/analysis-summary.txt), [영상 6회](../../work/implementation-speed-20261005/live-comparison/b1-summary.txt).

| 시험 | 기존 | 수정 | 판단 |
|---|---:|---:|---|
| 고정 원문 35개 × 각 3회, 번역 p50 | 383.59ms | 187.20ms | 모델/본문을 바꾸지 않고 반복 초기화 제거 |
| 같은 시험 HTTP 밖 p50 | 195.82ms | 0.123ms | 비용 위치 확인 |
| 영상 각 3회, 번역 p50 | 469.0ms | 250.0ms | GPU ASR와 함께 실행한 결과에서도 개선 |
| 영상 번역 p95 / 최대 | 684.8 / 750ms | 468.8 / 500ms | 긴 쪽 지연도 개선 |
| 영상 첫 한국어 자막 중앙값 | 5.329초 | 5.109초 | 남은 첫 4초 분절 대기는 그대로 |

고정 원문 105쌍은 요청 본문 해시가 모두 같고 오류/재시도는 0건이었다. 같은 seed에서도 최초 2쌍은 다른 번역이 나왔으며 초기 캐시 조건과 함께 관측됐다. 첫 비교의 두 블록을 제외한 70쌍은 모두 같은 결과와 약 0.197초 절감이 유지됐다. 비교용 seed를 제품 설정에 추가한 것은 아니다.

영상 6회 모두 ASR 원문 35개·입력 구간·최종 원문 순서·번역 원문/문맥이 같았다. 정확 대응 17구간 × 3회는 모두 더 빨랐고 대응 절감 중앙값은 220ms다. 처리 오류/새 누락·중복은 이 표본에서 관측되지 않았다. 기존 인명 오류 등까지 해결된 것은 아니다. 한국어 문자열 자체는 원래 샘플링 때문에 80/105쌍만 일치했다.

**재시도 조건:** HTTP/서버 수명·모델 교체·키/주소·런타임이 바뀌거나 지연 회귀가 발생할 때. 같은 client를 요청마다 다시 만드는 설계로 되돌리지 않는다.

### 7.2 A3 — 자막 교체 예약: 적용 완료

근거: [A3 검증](../../work/implementation-speed-20261005/A3-caption-scheduler/verification.txt).

가짜 시계/실제 WPF Dispatcher 새 검사 24개 + 기존 두 줄 36개 + 큐 24개, 총 **84개 통과**. 위치 타이머를 멈춰도 읽기 만료 후 진행하고, 조기/늦은 callback·삭제·교정·숨김 후 재표시·드래그·Clear/Closed를 확인했다. 큐 정책 소스는 그대로다. 화면 표시 시간을 측정한 시험은 아니다.

**재시도 조건:** Dispatcher 수명, 큐/읽기 정책, 자막 삭제/교정 경로를 바꿀 때 관련 회귀만 실행한다. 단순 위치 추적 타이머의 주기를 더 짧게 만드는 방식으로 대체하지 않는다.

### 7.3 B2 — Qwen encoder CPU 길이 변환 축소: 미채택

근거: [B2/B3 종합](../../work/implementation-speed-20261005/qwen-experiments/B2_B3_종합결과.txt), `attention-three-rounds/report.json`, `paired-analysis.json`, `attention-profile/summary.json`, `cpu-equivalence.json`.

같은 길이 텐서의 `tolist()`를 Q/K/V마다 호출하던 부분을 층마다 한 번 또는 encoder 전체 한 번으로 줄였다. 생산 라이브러리를 직접 고치지 않고 격리했다.

- CPU FP32/BF16·패딩·길이·배치 26개는 bit-exact.
- 실제 35구간 × 기존/두 후보 × 3회 = 315회 원문 동일.
- ASR p50: 기존 **294.15ms**, 층별 **293.32ms**, encoder **293.11ms**.
- 첫 회차를 제외한 문장별 대응 중앙 차이는 층별 +0.158ms, encoder -0.123ms. 회차별 방향도 바뀌었다.
- DtoH 복사 횟수 73→25→2회. 순수 GPU 복사 약 0.06ms와 CPU 동기화 시간은 별개이며, 실제 시간 비교에서 일관된 유의미한 이득이 없었다.

**판단:** 별도 HF 패치를 유지할 가치가 부족해 미적용. **재시도 조건:** 다른 GPU/버전/입력 길이·배치에서 profiler가 병목을 새로 입증하거나, 별도의 컴파일 최적화에 필요한 변경이라는 새로운 가설이 있을 때. 같은 35구간의 같은 패치를 다시 실행하지 않는다.

### 7.4 B3 — Qwen 컴파일: 가속 확인, 기본 적용 보류

근거: [B2/B3 종합](../../work/implementation-speed-20261005/qwen-experiments/B2_B3_종합결과.txt), [캐시 재사용 결과](../../work/implementation-speed-20261005/qwen-experiments/B3_캐시재사용결과.txt), `compile-forward/`, `compile-forward-all-cached/`.

work의 작은 격리 Python + Triton/헤더에서 기존 torch/모델을 원본 참조했다. 생산 Python 설치나 전역 CUDA/MSVC 설치를 바꾸지 않았다. 공식 forward compile만 시험했고 StaticCache 변경은 섞지 않았다.

| 시험 | eager | compile |
|---|---:|---:|
| 6구간, 준비 후 18회 ASR p50 | 213.61ms | 106.32ms |
| 같은 시험 첫 패스 합계 | 1.43초 | 115.13초 |
| 새 프로세스 + 위 캐시, 35구간 이후 105회 p50 | 290.76ms | 122.91ms |
| 같은 시험 반복 p95 | 581.44ms | 195.59ms |
| 같은 시험 첫 35구간 합계 | 10.54초 | 60.07초 |

첫 시험은 0.9초 입력에 **73.59초**, 다음 길이에 **41.06초**가 걸렸다. 캐시 재사용 시험도 첫 4초 입력 **48.20초**, 새 2.86초 입력 **8.17초**의 정지가 있었다. 이 시험은 앞선 6구간의 캐시를 재사용하고 입력 순서를 바꾼 조건이며, FX cache hit/miss는 각각 13이었다. 모든 그래프를 미리 준비한 캐시가 아니므로 앱 재시작마다 항상 48초가 걸린다고 일반화하지 않는다. 후속 compiled 경로 140회 원문은 eager와 같았다. unique_graphs 27, 재컴파일 한도와 일부 eager fallback도 관측했다.

**판단:** 반복 계산의 가속 가능성은 확인했다. 그러나 현재 예열 그대로 켜면 실시간 중 정지하므로 생산에 미적용. ASR-only이며 HY와 같은 GPU에서 동시에 돌린 전체 자막 시험이 아니다. ‘최초 설치 한 번만 느리다’고 설명할 수 없다.

**재시도 조건:** 세션 시작 전에 선행 컴파일을 완료하고, 새 길이/새 프로세스에서 정지하지 않도록 재추적·shape 처리를 바꾼 후보가 생겼을 때. 그 뒤 cold/warm, 미경험 입력, HY 동시 사용, 영어/일본어/자동 감지, 메모리·취소·모델 교체를 확인한다. 같은 `torch.compile(model.forward)`만 다시 켜는 실험은 반복하지 않는다. StaticCache는 **아직 시험하지 않은 별도 변수**다.

### 7.5 이전 ASR·경계·모델 시험

서로 다른 소스 버전/PCM/준비 조건을 최신 B1 수치와 직접 빼서 개선량으로 계산하지 않는다. 과거 실험은 대부분 한 중국어 영상의 38개 cue/277자 기준이다.

| ID / 실험 | 관측 결과 | 결정·반복 방지 / 재시험 조건 | 근거 |
|---|---|---|---|
| ASR-V2 경계 재확인 | CER 15.16→12.64%, 첫 자막 6.171→7.656초, ASR 26→41회 | 옵션 유지, **기본 꺼짐**. 처음만 늦는 기능으로 설명하지 않음. 속도 비용 없이 품질을 올리는 새 방법이 생길 때 재검토 | [v2 summary](../../work/asr-refinement-v2/summary.json) |
| ASR-V3 Qwen 안정 확정 최종 후보 | CER 15.16→10.83%, 첫 자막 5.922→9.922초. 공통 13개 cue 중 6개는 빨라지고 7개는 느려짐, paired 중앙 +0.265초 | 기본을 안정 모드로 바꾸는 무조건적 가속안은 제외. 중간 `stable-qwen`의 CER 9.75%/첫 9.86초와 최종 결과를 섞지 않음 | [최종 비교](../../work/asr-streaming-v3/final-comparison.json), [의미 검토](../../work/asr-streaming-v3/final-qwen-qualitative.json) |
| ASR-V3 엄격 신경망 VAD | 중간 stable CER 9.75%·삭제 4자 → VAD 후보 CER 20.22%·삭제 32자 | 신경망 음성 판정을 시작/PCM 소유 조건으로 삼은 방식은 누락이 늘었음. 현재 RMS 소유+종료 힌트를 유지. 조용한 말·짧은 응답의 정답 검증 없이 재도입하지 않음 | [VAD 비교](../../work/asr-streaming-v3/vad-comparison.json) |
| ASR-V4 최대 길이 4→3초 | 첫 자막 5.922→4.610초, CER 15.16→19.13%, 삽입 21→34자 | **단순 임계값 축소 미채택.** 새 경계·문맥·중복 대책 없이 같은 변경을 반복하지 않음 | [3초 비교](../../work/asr-speed-v4/three-second-comparison.json) |
| ASR-V5 병목 조사 | 당시 26개 구간 모두 4초 한도 종료. RMS 활성 98.50%, neural 활성 38.73%. 파일 입력 일정 대비 ASR 시작 잔여 대기 중앙 10ms/최대 22ms | 당시에는 음악 때문에 종료를 못 찾는 문제가 컸음. 무조건 GPU 큐/번역 모델 문제로 결론 내리지 않음. neural 종료를 정답으로 간주하는 것도 금지 | [분절 조사](../../work/asr-audit-v5/segmentation-audit.json), [대기 조사](../../work/asr-audit-v5/engine-backlog-check.json) |
| ASR-V6 neural 무음마다 조기 인식 | ASR 63회, speculative 30회 중 stale 27회 | 불필요한 계산 증가. 현재처럼 **실제 RMS 무음도 있을 때만** 사전 계산. 호출수를 확인하지 않고 주기를 더 줄이지 않음 | [v6 비교](../../work/asr-implementation-v6/comparison.json) |
| ASR-V6 `qwen-fast-rms-warm` | ASR 35회, speculative 2회/stale 0회, CER 15.884%, 첫 5.359초 | FIR·빠른 종료·RMS 한정 사전 계산·예열을 채택. 원문 품질의 대폭 개선이라고 과장하지 않음 | [적용 판정](../../work/asr-implementation-v6/acceptance.json) |
| ASR-NEMOTRON 동일 PCM | 320ms 설정 CER 41.16%, 1120ms 설정 37.91% | 비교만 실시, 생산 미채택. 해당 언어의 품질 개선 근거가 있을 때 재검토. 처리 청크 320ms를 한국어 자막 지연으로 설명하지 않음 | [Nemotron 비교](../../work/asr-implementation-v6/nemotron-comparison.json) |
| ASR-V7 seam 확장 | `comparison.json`은 v7 run-report 미완성/pending | **중단·미완료**. 향상 확인으로 기록하지 않음. helper가 존재한다고 생산 경로에 연결됐다고 가정하지 않음 | [v7 상태](../../work/asr-boundary-v7/comparison.json) |
| BOUNDARY-20261005 짧은 연속 꼬리 | CPU 합성 20/100/180ms 꼬리가 ASR까지 전달. 실제 영상의 입력/ASR/최종 원문·CER 15.884% 동일 | **누락 가능성 회귀 수정 채택.** 영상에 해당 꼬리 사례가 없어 실제 음성 복구 효과는 미측정. 새 독립 200ms 최소 완화와 혼동 금지 | [CPU](../../work/boundary-test-20261005/cpu-result.json), [적용](../../work/boundary-test-20261005/applied-result.json) |

주의할 기록의 시점·범위:

- 경계 `pair-check.json`의 `candidate_applied=false`는 적용 전이다. 이후 `applied-result.json`이 실제 반영과 적용본 검사 77개 통과를 확정한다. LF/CRLF 차이를 기능 차이로 오인하지 않는다.
- v7의 `seam_repetition()` helper/단위 검사는 있지만 생산 호출자가 없다. 현재 FastQwen은 `remove_forced_overlap()`를 사용한다. 2~3자 중복을 추가 ASR로 해결했다고 쓰지 않는다.
- 현재 강제 중복 비교는 간번체/구두점을 정규화하고 CJK 4자 이상을 대상으로 한다. 완전 반복 문장·반복 패턴·2~3자는 보존한다. 단어 시각 없이 실제 반복과 중복을 항상 판별하지 못한다.
- v6 `qwen-old-capture`의 원래 `success=false`는 최초 해시 수집 후 metadata sidecar가 생긴 이유를 포함한다. 파이프라인은 성공했고 나머지 보호 파일/실제 PCM 불변을 별도로 확인해 비교 가능한 것으로 분류했다. 원본 상태를 임의로 성공으로 덮어쓰지 않는다.
- Nemotron 초기 잘못된 tokenizer 결과는 비교에서 제외했다. 올바른 `processor.decode`와 동일 PCM 결과를 사용한다. SRT에 없는 영상 말미 LiTV 음성을 무조건 환각으로 세지 않는다.
- 안정된 원문 전체를 합치면 맞아도 분리 번역에서는 “그냥 이대로 둬 / 부숴버려”처럼 의미가 달라질 수 있다. CER만으로 자막 의미 품질을 판정하지 않는다.

### 7.6 Whisper 정체·확정 및 빠르지 않았던 대안

**WHISPER-STALL — 적용한 중단 방지**

근거: [정체 수정 기록](validation/streaming-stall/README.txt), `validation/streaming-stall/same-audio.json`, 수정 전 `work/streaming-stall-20261004/`.

배경음의 RMS가 계속 양수인 상태에서 흔들리는 단어 시각, 긴 segment, 반복 ASR-empty, 짧은 안정 구절 때문에 오래된 PCM이 회수되지 않아 24초 버퍼 한도에 도달하던 문제가 있었다. 현재는 8초 진행 정체/12초 단일 segment deadline, empty 반복 시 오래된 오디오 회수와 최근 문맥 보존, 실제 시각 경계만큼만 PCM을 제거하는 처리가 있다.

당시 실제 40초의 `중국어 → 배경음 → 같은 중국어` 시험은 구형 실패/수정본 성공, 두 발화 보존(CER 0/26), 최대 버퍼 7.55초였다. 35초 배경음만 입력한 시험은 자막 없이 계속 동작했다. **deadline·empty 회수를 제거하거나, 같은 말이라는 이유로 두 번째 발화를 삭제하지 않는다.** 새 정체 표본과 원문 보존 근거가 있을 때만 정책을 바꾼다.

**WHISPER-PROFILES — 기본 교체는 보류**

과거 같은 비교의 첫 자막/CER은 legacy **2.609초/23.47%**, stable **5.875초/23.10%**, AlignAtt 구절 수정본 **5.969초/35.02%**였다. AlignAtt ASR p50이 0.125초여도 전체 자막이 더 빠르거나 정확하지 않았다. 구절 수정 전 AlignAtt의 첫 13.829초를 수정 후 결과와 섞지 않는다. 최신 B1 코드와 직접 속도 비교할 값도 아니다. 근거: [v3 최종 비교](../../work/asr-streaming-v3/final-comparison.json).

새 beam/VAD/정렬 예열·수집 분리 변경은 아직 후속 후보다. 단어 시각·실제 반복·잠정 교정·취소를 보존하고 별도 A/B를 해야 한다. 단순 `chunk_length`, `num_workers`, batch 확대, temperature fallback, 인식 간격 축소가 실시간 한 사용자에 자동 이득을 준다고 가정하지 않는다.

### 7.7 번역 모델·프롬프트·오류 복구 이력

| ID / 실험 | 확인한 내용 | 현재 결정 / 다시 검토할 조건 | 근거 |
|---|---|---|---|
| MT-RECOVERY | 당시 Qwen3.5-4B 합성 16문장에서 `Hmm.`/`…` 실패로 14/16 처리 → 수정 후 16/16 처리 | 문장별 출력 실패와 전체 세션 장애를 분리한 처리를 유지. Gemini 사용 중이어도 로컬 MT 실패인지 구분 | [검증 기록](validation/translation-recovery/검증기록.txt) |
| MT-HY-LABEL | `〖待翻译文本〗`를 답에 복사. 기존/한글 강화 지시 각 3회 실패, 무제목 simple 3회 통과 | 원문 앞 제목을 제거한 현재 정책 유지. 프롬프트 변경 시 이 사례 포함 | [probe](validation/streaming-improvements/translation-probe.json) |
| MT-HY-EOS | 고정 Q6_K EOS=3은 `$`, 실제 종료 토큰은 127960. 강제 토큰 생성으로 구별 | 특정 파일의 in-memory override 유지. 파일 바이트 수정·전 모델 일괄 override 금지 | [바이너리 검증](../../work/hy-mt2-setup/binary-template-validation.json), [종료 토큰 생성 검사](validation/hy-mt2-7b/saved-text-probes.json) |
| MT-TG-HY-56 | 저장 원문 56개에서 TranslateGemma 54/56, HY 56/56 출력 검사 통과. 당시 중앙 0.453/0.390초 | **설치/출력 검증**이며 의미 정답률·현재 모델 속도 순위 아님. 원문 오인식 포함. 같은 로그만으로 모델 우열 재선언 금지 | [TranslateGemma](validation/translategemma-12b/saved-text-probes.json), [HY](validation/hy-mt2-7b/saved-text-probes.json) |
| MT-TG-BOS | 토크나이저 자동 BOS 1개 확인 | raw 프롬프트에 BOS를 무조건 추가하지 않음 | [템플릿 검증](../../work/translation-gemma-setup/binary-template-validation.json), [토크나이저 검사](validation/translategemma-12b/saved-text-probes.json) |
| MT-QWEN9B | 설치 검증 성공, 번역 사례 11/12·13요청. 한자 잔존, 일본어 이중부정/허용 표현 오역 | ‘설치 성공=상위 품질 검증’ 아님. 대안 3프롬프트×5개 시험은 `production_prompt_changed:false`로 미채택 | [acceptance](../../work/qwen35-9b-setup/acceptance.json), [prompt probes](../../work/qwen35-9b-setup/prompt-probes.json) |
| MT-MILMMT-BATCH | 14문장 출력 확인, 의미 점수 없음. ASR 동시 실행의 GPU 사용량 15,603→14,071MiB, 짧은 검사 0.755→0.553초 | microbatch 128 채택 목적은 동시 VRAM 여유. 한 번의 짧은 결과를 일반 속도 보장으로 확대하지 않음 | [검증](../../work/milmmt-12b-setup/verification.json), [동시 실행](../../work/milmmt-12b-setup/concurrent-verification.json) |

한국어로 된 거절·오역은 문자 검사에 통과할 수 있다. TranslateGemma의 성적 내용 번역 거절은 사용자 보고가 있었지만 저장된 위 시험이 이를 재현·해결했다고 볼 근거는 없다. 정답 문장·모델·프롬프트가 고정된 별도 사례가 있어야 다시 판정한다.

**로그 비교 함정:** `work/log-comparison-20261004/current-capture.json`의 88행은 두 세션 45+43행이다. 앞의 45행 세션은 `google-capture.json`에 그대로 포함된다. 이를 독립적인 ‘로컬 88 대 Google 45’ 표본으로 비교하지 않는다. 파일명만으로 ASR 공급자·동일 영상 구간을 확정하지 말고 세션과 실제 원문을 확인한다.

모델 선택 검토 시 `MainWindow.xaml.cs`의 SmokeTestAsync 가짜 모델 목록을 실제 목록으로 오인하지 않는다. 정상 UI는 `/v1/models`를 읽는다. 현재 파일 목록과 과거 설치 시험 목록은 별개이며, 이 문서는 과거 모델을 자동 재설치하라는 지시가 아니다.

### 7.8 리샘플러·오버레이 회귀 이력

- **CAPTURE-FIR:** 구형 선형 변환은 48kHz의 9/10/12kHz를 7/6/4kHz로 aliasing했다. `work/asr-audit-v5/resampler/production-results.json`은 그 **구형** 문제 재현이다. 현재 FIR의 근거는 [v6 변환 검사](../../work/asr-implementation-v6/resampler/README.txt)와 `results.json`의 80개 assertion이다. 예: 진폭 0.5의 9kHz 입력 alias 진폭 약 0.0000217. 합성 PCM 증거이며 모든 실방송 ASR 향상 증명이 아니다. 출력 길이/drift/불규칙 패킷/discontinuity/끝 음소 보존 없이 단순 변환으로 되돌리지 않는다.
- **OVERLAY-PLACEMENT:** 과거 브라우저/모니터 정렬과 재시작 세로 위치, 숨긴 뒤 재표시, 메뉴 없는 팝업 문제를 수정했다. 최신 자유 배치 근거는 [22개 검사](../../work/overlay-free-placement-tests/run-edge-final/free-placement-report.json), 재시작 수정은 [restart report](../../work/overlay-restart-tests/run-fixed/restart-report.json)다. 물리 xywh 즉시 저장, DPI, 재실행, 브라우저 이동/최소화, 두 preset의 위치/크기 초기화를 보존한다.
- 옛 `overlay-monitor-tests/run-fixed`의 90%는 **당시 정책**이다. 현재는 95%다. 예전 드래그 후 자동 가로 중앙 정렬을 현재 자유 배치 정책으로 되돌리지 않는다.
- **OVERLAY-TWO-LINES:** 한 문장씩 너무 빨리 교체되는 문제 때문에 두 줄과 읽기 시간/순서 보존을 넣었다. 긴 자막의 줄바꿈 대신 한 줄 축소, 수정 ID의 시계 유지, 숨김 뒤 새 시작 규칙을 함께 검증한다. A3는 이 위에 예약만 바꾼 것이다.
- **SCREEN-TIMING:** 종단 화면 시각 계측/render acknowledgement는 사용자 요청으로 제거했다. 과거 `work/subtitle-latency-capture`가 남아 있어도 재도입 근거가 아니다. 일반 `DiagnosticState`와 opt-in smoke 검사는 남아 있으므로 ‘진단 코드가 전부 제거됐다’고 쓰지 않는다.

### 7.9 참고 공개 소스와 가져오지 않은 방식

| 참고 | 실제 참고/포함 범위 | 다시 조사할 때 주의 |
|---|---|---|
| LiveCaptions-Translator `a6fee12757b15edbeef7c60f8895e0be694801e1` | Windows LiveCaptions.exe의 출력을 UI Automation으로 읽는 번역 프로그램. 인식 확정과 번역 표시 분리를 참고 | 자체 PCM ASR/VAD 종료 알고리즘 소스가 아님. 새 번역 완료 시 앞 작업을 일괄 취소하는 방식을 가져오면 별도 발화를 버릴 수 있음 |
| Whisper-Streaming | LocalAgreement, 계산 중 도착한 오디오 수집·확정 구조 참고 | 이 프로젝트의 서버 전체를 사용한다고 설명하지 않음 |
| WhisperLiveKit `363e4f6d029694d9c81ae548beddd9d3c88a3637` | 현재 `_vendor`는 AlignAtt decoder, CT2 bridge, tokenizer/timing 등 일부 | 원본 서버/UI/번역/가중치 전체를 포함한 것이 아님. 출처·자산·라이선스 유지 |
| SimulStreaming `077ea37d5ab4ff98bc567e4507f140dc4e5d5ad6` | 다운로드해 비교한 알고리즘·구현 참고 | 다운로드 revision과 vendor의 중간 조상/라이선스 출처 revision을 동일하다고 단정하지 않음 |
| RealtimeSTT | 무음 중 사전 인식·발화 재개 시 무효화·결과 재사용 참고 | 이 원리는 현재 Qwen 빠른 경로에 이미 대응 구현됨. tail 재인식은 추가 계산이 필요해 무비용 seam 개선안이 아님 |
| Qwen3-ASR-causal 고정 참고 버전 | 영어용으로 별도 학습된 모델 비교 | 당시 README의 중국어 CER windowed 11.4→causal 85.7. 현재 Qwen1.7B 중국어용으로 단순 교체할 최적화가 아님 |

근거: [공개 프로그램 분석](../../work/public-subtitle-review-20261004/analysis.ko.txt), [vendor 출처](engine/_vendor/whisperlivekit/UPSTREAM.json), `work/upstream-asr-20261004/manifest-WhisperLiveKit.json`, `manifest-SimulStreaming.json`, [기술 검증](../../work/research-logic-20261005/검증_및_우선순위.txt).

같은 revision을 다시 내려받아 같은 기능을 신규 발견으로 취급하지 않는다. 상위 소스가 바뀌었거나 현재 미해결 문제에 직접 대응하는 변경이 생겼을 때만 차이를 확인한다. 논문의 GPU/batch/언어 조건과 현재 Windows batch1 실험을 구분한다.

## 8. 검토했지만 아직 적용하지 않은 후보

근거: [기존 후보](../../work/research-logic-20261005/기존_개선후보.txt), [기술 검증](../../work/research-logic-20261005/검증_및_우선순위.txt), [최종 소스 재검토](../../work/review-final-20261005/최종소스_재검토.txt).

**시점 주의:** 위 과거 문서의 B1 ‘효과 미확인’, A3 ‘구현 후보’는 뒤의 실제 적용·실험으로 갱신됐다. A5는 공식 process-loopback의 무음 공급 사양을 확인한 뒤 우선순위를 낮췄다. 과거 의견을 최신 결론처럼 다시 사용하지 않는다.

**후속 조사:** 2026-10-05 중국어권·영어권 개발자 커뮤니티 재조사에서 남긴 후보와 변경된 우선순위는 §11을 먼저 확인한다. 아래 후보표는 이력 보존용이며 전부 실행하라는 목록이 아니다. 특히 B5 전송 묶음 축소는 이번 조사에서 후순위로 낮췄다.

| ID | 후보 / 현재 상태 | 다시 진행할 가치가 생기는 조건 |
|---|---|---|
| A1 | Whisper legacy 음성 공급/추론 분리. CPU 부하 재현만 완료 | Whisper에서 실제 밀림이 있을 때 원문 보존·취소와 GPU 전후 비교. Qwen에는 이미 분리 구조가 있음 |
| A2 | 교체된 잠정 번역의 내부 품질 재시도 생략. CPU 재현 완료 | Whisper/Gemini 같은 ID 교정 경로에 적용·검증. 다른 확정문 취소 금지 |
| A4 | Whisper 압축비 >2.4의 정상 반복 삭제 위험. 모의 응답 재현 | 실제 정상 반복과 무음 환각의 정답 음성으로 판정. 필터 전체 해제 금지 |
| A5 | 마지막 발화 뒤 PCM 공급 중단 시 미확정 가능성 | 실제 입력 간격 로그로 먼저 입증. 추정만으로 타이머/가짜 무음 주입을 추가하지 않음 |
| A6 | 문자 seam 중복 제거의 실제 반복/짧은 중복 모호함 | 오디오·시간 근거와 정답 표본 필요. 4자 기준을 2자로 낮추는 일괄 변경 금지 |
| A7 | AlignAtt 짧은 확정 꼬리의 장기 보류 | 해당 옵션 사용 시, 추가 ASR 없는 배출과 번역 단위/호출수/정확도를 비교 |
| B4 | Whisper 예열의 VAD/단어 정렬 경로 보강 | cold 시작의 준비 시간+첫 자막 합계로 평가. 전체 문장 가속으로 주장하지 않음 |
| B5 | 로컬 PCM 100→20/40ms | 앱 60패킷·서버 120메시지의 개수 제한도 함께 검토하여 6초 보존량 유지·샘플 보존·CPU 부하를 비교. Gemini의 100ms 권장 경로는 구분 |
| B6 | Whisper beam 5→3/1 | 속도뿐 아니라 CER/누락/가설 안정성/재인식 횟수 비교 필요 |
| B7 | Whisper 종료에 연속 VAD 보조 | 음악·조용한 말·짧은 응답 자료에서 RMS 종료와 별개로 검증 |
| B8 | Qwen 끝 무음 0.5→0.35/0.4초 | 짧은 쉼의 오분절 검증 필요. 연속 발화의 4초 경계에는 같은 이득이 없음 |
| B9 | Qwen 새 독립 발화의 200ms 최소 완화 | 짧은 실제 말·클릭/효과음의 정답 표본 필요. 이미 수정한 연속 꼬리와 다른 기능 |
| B10 | 번역 stream/prompt 재배열/병렬 수 조정 | 실제 단계별 병목을 입증한 뒤 품질·재시도·읽기 정책까지 비교 |
| B11 | Gemini hybrid VAD / 장시간 세션 전환 | 실제 API/계정 환경의 별도 시험 필요. 현재 로컬 속도 개선 수치에 포함하지 않음 |

## 9. 앞으로의 검증·빌드·배포 절차

### 시험 설계

1. 현재 소스/설정/모델 출처·버전·해시를 고정한다. 키의 실제 값은 기록하지 않는다. 백업에는 모델을 복사하지 않는다.
2. 변경 변수는 하나씩 분리한다. 모델 계산, 분절 정책, 번역 프롬프트를 동시에 바꾸고 어느 변경이 효과가 있었는지 추정하지 않는다.
3. 동일 파형·언어·힌트·모델·설정으로 AB/BA 순서를 바꾸며 반복한다. 최근 기준은 양쪽 최소 3회이며 편차가 크면 추가한다.
4. cold 준비/첫 추론/warm 반복, ASR 호출수, 문장 확정 대기, MT 큐, 서버 계산, 전체 이벤트 도착을 분리한다.
5. CER/WER, 인명/숫자/부정, 누락·중복, 번역 실패, 의미를 함께 본다. 실제 입력 구간/원문이 바뀌었는지도 비교한다.
6. 모델·GPU 없이 재현 가능한 수명/큐/취소 문제는 CPU 검사로 먼저 확인한다. GPU 프로파일링 실행을 속도 측정 실행과 섞지 않는다.
7. p95/최대·메모리·취소/종료·회귀까지 통과한 변경만 기본값에 적용한다. 처리 속도와 준비 시간의 손익을 따로 기록한다.
8. 시험 종료 후 소유 프로세스 정리, 설정·기록 보존, 실제 적용 소스와 시험 후보의 동일성을 확인한다. 영상에 없는 경계 사례는 합성 검사만 검증됐다고 표시한다.

### 최근 실제 통과 기록

- B1 적용 후 전체 엔진: **1010 passed, 1 skipped, 1 warning / 15.82초**. [실행 기록](../../work/implementation-speed-20261005/engine-tests.txt). 경고는 Starlette TestClient의 httpx 지원 중단 예정 안내.
- A3: 새 24 + 기존 60 = **84개 통과**, Release 빌드·publish 성공.
- 이전 경계 수정: 실제 적용 소스 관련 **77개 통과**. 이것을 최신 전체 1010개에 중복 합산해 새로운 총 검사 수로 보고하지 않는다.
- 이전 V6의 918개, 후속 1000개 등은 당시 소스의 기록이다. 지금 다시 돌렸다는 뜻으로 인용하지 않는다.
- 이번 문서 작성 때문에 전체 GPU/CPU 제품 테스트를 재실행할 필요는 없다. 문서 경로와 내용 일치만 확인한다.

엔진 회귀 명령(앱 폴더에서 실행):

```powershell
.\runtime\python\python.exe -B -X utf8 -m pytest engine/tests -q -p no:cacheprovider
```

모델 시험은 `work/implementation-speed-20261005/run.py`, `fixed_translation.py`, `qwen-experiments/bench_asr.py`, `compile_bench.py` 등의 저장된 runner와 입력 manifest를 먼저 확인한다. 과거 출력 디렉터리를 덮어쓰지 않는다. `--allow-gpu` 등의 안전 조건을 임의로 없애지 않는다.

### 빌드·실행본 반영

- [build.ps1](build.ps1)과 [SOURCE_RELEASE.txt](SOURCE_RELEASE.txt)를 사용한다. WPF는 `net10.0-windows`, 실행본은 `win-x64`, self-contained, `PublishSingleFile=false`다.
- **publish 출력 폴더를 프로젝트의 상위인 앱 루트로 직접 지정하지 않는다.** SDK의 소스 glob 제외 때문에 앱 소스가 누락된 빌드가 나올 수 있다. 별도 staging에 publish한 뒤 반영한다.
- 가장 최근 반영은 C: 앱의 `LiveSubtitle.dll`/`.pdb` 교체이며 `.exe` 호스트는 그대로 사용했다. [복사 전후 해시](../../work/implementation-speed-20261005/app-publish-changes.json)가 있다.
- 소스 변경만 한 상태, CPU 검사만 한 상태, publish까지 적용한 상태를 구분해 기록한다. 실행 중인 앱이 새 코드를 사용한다고 가정하지 않는다.

### 배포·GitHub

- 프로젝트 코드/문서는 MIT, 외부 라이브러리·모델은 각각의 라이선스다. `_vendor` 출처와 원문 고지를 유지한다.
- Code는 소스/필수 외부 소스·라이선스/예시 설정, Releases는 일반 사용자용 실행본으로 구분하는 [업로드 계획](../../work/github-publication-20261005/업로드_계획.txt)이 있다. **계획 작성과 실제 게시를 구분한다.**
- runtime/모델 가중치/config/로그/실험 영상/토큰/키가 포함된 실행 폴더를 통째로 공개하지 않는다. `.gitignore`가 ZIP을 걸러주는 것은 아니다.
- 큰 실행본 크기를 모델 가중치만의 문제로 설명하지 않는다. PyTorch/CUDA, Whisper용 NVIDIA 라이브러리, llama.cpp CUDA가 각 실제 경로를 담당한다. 제거는 해당 기능 의존성을 확인한 뒤 결정한다.
- 모델 다운로드 안내·부속 파일과 실제 가중치를 구분한다. 기존 `0_사용법`을 유지하고 같은 사용법 폴더/문서를 중복 생성하지 않는다.

## 10. 이후 변경 기록 양식

새 결과는 아래 형식으로 추가하고 위 상태표와 현재 구현 설명도 함께 갱신한다. 이전 결과/실패 근거는 보존한다.

```text
ID / 날짜:
문제와 재시도 이유:
기존 관련 실험 ID·결론:
이번에 달라진 가설/조건:
변경한 소스·설정(딱 무엇을 바꿨는지):
기준 소스/후보 소스/모델/런타임/하드웨어:
입력 데이터·언어·힌트·PCM/SRT 해시:
시험 범위: CPU 모의 / 실제 ASR / 실제 MT / 엔진 재생 / UI 논리
반복 횟수·실행 순서·cold/warm·캐시 조건:
결과: 원문/번역 변화, CER/WER, 호출수, p50/p95/최대, 오류/누락/중복, 메모리
준비 시간과 방송 중 지연:
적용 / 선택 기능 / 보류 / 원복 / 미완료 및 이유:
실제 실행본 반영 여부·설정/가중치 보존·소유 프로세스 정리:
원시 로그·분석 스크립트·요약 경로:
남은 한계와 다시 시도할 조건:
```

## 11. 중국어권·영어권 개발자 커뮤니티 재조사 — 2026-10-05

**이 절은 시험 전 조사 기록이다. 최신 상태는 §12를 우선한다.** 이후 CR1·CR2·CR3 및 Whisper CR5의 격리 구현·시험을 마쳤으며 모두 제품 미채택이다. CR4는 사용자 지시로 제외했다. 아래의 후보·권장 순서는 당시 가설로 보존하며, 같은 조건의 실험을 다시 시작하라는 지시가 아니다.

목표는 새 기능 수를 늘리는 것이 아니라, 기존 실패 조건을 반복하지 않으면서 **현재 Qwen3-ASR-1.7B + HY-MT2-7B의 중국어→한국어 자막**에 도움이 될 가설을 추리는 것이다. Windows RTX 4080 16GB, 방송 중 지연 악화 금지, 입력 힌트 비필수, 원문/확정문 보존, 두 줄 읽기 정책을 기준으로 걸렀다.

### 11.1 검색 범위와 근거 수준

| 구분 | 확인한 커뮤니티·개발자 자료 | 이번 판단에 사용한 범위 |
|---|---|---|
| 중국어권 | [V2EX OpenASR 저자 소개·답변](https://www.v2ex.com/t/1228707?p=1), [LiveTranslate](https://github.com/Dehydrated-gumarabic195/LiveTranslate), [six-ddc/livecaption](https://github.com/six-ddc/livecaption) | 중국어 개발자의 실제 사용 문제와 소스 구현. 한국어 번역 품질 검증으로 간주하지 않음 |
| 영어권 | [Silero 분할 논의](https://github.com/snakers4/silero-vad/discussions/364), [관련 PR #664](https://github.com/snakers4/silero-vad/pull/664), [Silero 상태·성능 논의](https://github.com/snakers4/silero-vad/discussions/804), Whisper/RealtimeSTT 이슈·Discussion | 유지관리자 답변, 수정 코드, 실제 오류 사례. 파라미터만 맹목적으로 복사하지 않음 |
| 공개 구현·실험 보강 | [FireRedVAD](https://github.com/FireRedTeam/FireRedVAD), [Orin Qwen 최적화 보고서](https://github.com/star-nexus/G1-Robot-Agents-Speech/blob/main/docs/qwen3_asr_orin_nx_runtime_optimization_report_en.md), [OpenVoiceStream 비교](https://github.com/Seeed-Solution/openvoicestream/blob/main/docs/perf/qwen3-asr-rk-streaming-ab-20260601.md), [llama.cpp Discussion](https://github.com/ggml-org/llama.cpp/discussions/11348) | 포럼의 주장을 코드/측정 조건과 교차 확인. 장치·언어·모델이 다른 벤치를 우리 성능으로 환산하지 않음 |

중국어로 작성된 개발자 글·프로젝트와 영어 토론을 구분한 것이며 개발자 국적을 추정한 분류가 아니다. 출처의 ‘실시간’, ‘정확도’, ‘2배’만으로 채택하지 않는다. 이번에 확인한 자료 중 **우리 조합의 중국어→한국어 실방송 개선을 직접 입증한 자료는 없다.** 아래 순위는 시험 가치에 대한 판단이다.

상세 조사: [중국어권](../../work/community-research-20261005/chinese-research.txt), [영어권 ASR](../../work/community-research-20261005/english-asr-research.txt), [번역 파이프라인](../../work/community-research-20261005/translation-research.txt), [통합 담당 근거](../../work/community-research-20261005/root-research.txt). [파일별 최신 commit 메타데이터](../../work/community-research-20261005/source-revisions.json)는 이후 변경 여부 확인용이다. 웹 캐시와 API 조회 시점은 다를 수 있으므로 구현 시 사용할 revision을 다시 고정한다.

### 11.2 실제로 남긴 후보

| 우선순위 / ID | 시험할 가설 | 기존 기록과 다른 점 | 판단 |
|---|---|---|---|
| 1 / CR1 | 아주 짧은 원문에서 HY의 이전 문맥을 선택적으로 줄이기 | 현재 항상 사용하는 최대 3문맥을 입력 유형에 따라 제한. 라벨 재도입·추가 교정/재번역은 하지 않음 | 작은 고정 원문 시험부터 진행할 가치 있음 |
| 2 / CR2 | 4초 강제 분할 시 가까운 과거의 짧은 쉼을 분할점으로 선택 | 일괄 3초 단축, 미래 음성 재확인, 문자 중복 삭제와 다름 | 추가 ASR 없이 경계 품질을 바꿀 후보 |
| 3 / CR3 | FireRed **Stream-VAD**를 CPU의 종료 힌트로 비교 | 엄격한 VAD로 PCM을 삭제한 실패 실험과 다름. 오디오 소유권은 그대로 | 저장 음성의 감지 결과 비교부터, 통과 시 실제 ASR 비교 |
| 조건부 / CR4 | StaticCache와 생성 과정의 decoder 컴파일을 별도로 검토 | B3의 전체 `model.forward` 컴파일과 다른 경로. StaticCache는 기존 미시험 | 준비·가변 길이 정지 대책이 있는 격리 실험만 가치 있음 |
| Whisper 전용 / CR5 | `int8_float16`과 `float16` 계산 형식만 비교 | 현재 INT8 고정이며 B6 beam 조정과 별개. 새 ASR/추가 추론 없음 | Whisper를 실제 사용할 때 작은 비교 가치 있음 |

#### CR1 — 짧은 원문에 필요한 만큼만 번역 문맥 사용

근거: [livecaption 번역 구현](https://github.com/six-ddc/livecaption/blob/12bed7a7c03b972f1c34c6ca175acec073007a03/livecaption/translate.py). 해당 프로젝트는 짧은 조각에서 배경 문맥 자체가 번역되는 문제에 대응해 문맥을 생략하는 분기를 둔다. 그 밖의 재번역/잠정 경로까지 도입하자는 제안은 아니다.

현재 `translation_profiles.py`는 짧은 원문에도 최대 3문장 배경을 넣는다. **우리에게도 같은 오류가 발생하는지 고정 원문으로 먼저 확인**한다. 후보는 단독 맞장구·감탄사 같은 입력에서 문맥을 0 또는 1문장으로 제한하는 단일 번역 요청이다. 임계값은 아직 정하지 않았다.

- 중국어에 영어 공백 단어 수를 쓰지 않는다. `他呢？`, `不是他`처럼 짧아도 지시대상·부정에 문맥이 필요한 문장을 대조군에 둔다.
- 현재 HY 템플릿·샘플링·성공한 원문만 저장하는 정책을 유지한다. MT-HY-LABEL의 실패한 원문 앞 제목을 복원하지 않는다.
- 기존 35구간과 짧은 원문 표본에서 이전 문장 재번역, 인명/대명사/부정, 한국어 의미를 판정한다. 한국어 문자 검사 통과만으로 채택하지 않는다.
- 추가 추론은 허용하지 않는 후보다. 호출수, MT p50/p95·최대, prompt-cache 조건을 함께 비교한다. 문맥 토큰 감소만으로 속도가 개선됐다고 주장하지 않는다.
- 재현되는 문제도 없거나 문맥을 줄여 의미가 나빠지면 종료한다. 모든 문장의 문맥을 일괄 제거하는 재시험으로 확대하지 않는다.

#### CR2 — 최대 길이에 도달했을 때 분할 위치만 보완

근거: [Silero PR #664](https://github.com/snakers4/silero-vad/pull/664), [후속 분기 수정](https://github.com/snakers4/silero-vad/commit/23890394080ce74954a6ee588050bb52fa7028e8), 중국어권 Windows [LiveTranslate의 VAD 코드](https://github.com/Dehydrated-gumarabic195/LiveTranslate/blob/8f8f98930598bded35c41aeb4f604246161f651a/vad_processor.py). 최대 길이에서 이미 지난 쉼·낮은 음성 확률 구간을 찾아 절단하는 실제 구현이 있다.

현재 `fast_qwen.py:168–184`는 4초 위치에서 닫고 마지막 200ms를 겹친다. 제안은 **4초 상한을 유지하면서 마지막 수백 ms 안의 확인된 짧은 쉼만 후보로 사용**하는 것이다. 후보가 없으면 현재 동작을 유지하며 뒤쪽 PCM은 모두 다음 창으로 넘긴다. 최근 범위 제한과 무삭제 이월은 LiveSubtitle에 맞춘 설계 가설이다.

- 미래 음성을 추가로 기다리는 단계를 넣지 않는다. 하지만 다음 창으로 넘긴 음절의 자막은 늦어질 수 있으므로 ‘무지연 개선’이라고 부르지 않는다.
- 파형 전체에서 가장 낮은 확률이라는 이유만으로 말하는 도중을 자르지 않는다. 라이브러리의 무음 삭제/짧은 발화 필터까지 함께 복사하지 않는다.
- 기존 4→3초 실험의 품질 악화를 반복하지 않도록 원문 CER, 실제 반복, 부정어/인명과 번역 의미를 비교한다.
- 모든 샘플의 소유권·200ms 겹침 범위·20~180ms 꼬리 보존, ASR/MT 호출수와 지연 p95/최대를 확인한다. 별도 tail 재인식이나 강제 정렬 모델은 넣지 않는다.
- 적절한 쉼이 거의 없거나 호출수/지연만 늘면 보류한다. A6의 짧은 문자열 삭제 기준을 공격적으로 바꾸는 근거로 사용하지 않는다.

#### CR3 — FireRed Stream-VAD를 ‘종료 참고 신호’로만 비교

근거: [V2EX의 OpenASR 개발자 답변](https://www.v2ex.com/t/1228707?p=1), [공식 FireRedVAD 코드·사용법](https://github.com/FireRedTeam/FireRedVAD). 스트리밍 모델은 약 2.2MB이며 CPU 경로가 있다. 널리 인용되는 F1 97.57은 **비스트리밍 모델** 결과이므로 이 후보의 실시간 정확도 보장으로 쓰지 않는다.

현재 `OnlineSpeechGate` 자리에 들어갈 별도 어댑터 후보다. 처음에는 저장 PCM에 대해 실제 자막 경로에 영향을 주지 않고 확률·끝점만 비교한다. RMS 시작 정책, 원본 PCM 보존, 4초 상한, 전체 파형 비음성 게이트는 고정한다. **이 교체만으로 낮은 RMS 때문에 시작하지 못한 발화를 복원한다고 주장하지 않는다.**

- `Stream-VAD`의 raw/smoothed probability를 현재 끝점 정책에 연결한다. 모델 자체의 무음 종료 대기를 현재 500ms 뒤에 직렬로 더하지 않는다.
- 해당 특징 추출의 25ms 창/10ms hop에 맞춰 연속 입력 어댑터가 필요하다. 현재 20ms PCM 조각을 그대로 독립 특징으로 바꾸거나 프레임 경계 샘플을 버리지 않는다. 같은 음성의 청크 분할 방식을 달리해도 확률·끝점이 유지되는지 확인한다.
- 중국어 BGM/조용한 말/짧은 쉼에서 잘못된 종료와 끝점 오차를 비교한다. 영어·일본어를 중국어 결과로 대신 검증하지 않는다.
- CPU 실행 비용 p95, 초기화·상태 reset·마지막 부분 프레임을 먼저 확인하고, 통과한 경우에만 동일 Qwen+HY로 실제 누락/중복/지연을 판정한다.
- 공개 requirements의 옛 Torch/CUDA 고정 버전을 앱에 설치하지 않는다. 기존 Python/Torch와 격리 호환을 먼저 확인한다. 코드/가중치 고지와 사용 revision을 각각 기록한다.
- 과거 엄격 VAD 실험처럼 음성 아닌 프레임을 버리거나, FireRed·Silero를 항상 중첩 실행하는 방식으로 확대하지 않는다. 기존보다 느리거나 누락이 늘면 채택하지 않는다.

#### CR4 — 컴파일은 다른 경로와 준비 대책이 있을 때만

근거: [G1 Robot 개발자의 Orin NX 보고서](https://github.com/star-nexus/G1-Robot-Agents-Speech/blob/4412ab99420325c159f6a115d5afe3b7ebf9c32c/docs/qwen3_asr_orin_nx_runtime_optimization_report_en.md). `cache_implementation='static'`와 Transformers의 생성 과정에서 decoder를 컴파일하는 방식이다. 기존 B3에서 직접 묶은 multimodal `model.forward`와 구분한다.

보고 조건은 **Qwen 0.6B, Orin NX, FP16, 고정 중국어 한 표본**이다. 첫 실행 약49.9초, disk cache가 있는 새 프로세스 약17.56초도 보고돼 있어 준비 문제가 해결됐다는 증거가 아니다. 일부 연결된 상세 근거 파일은 열람되지 않아 재현성에도 한계가 있다.

우리 1.7B/Windows에서의 효과는 미확인이다. 모델 준비 단계의 선행 실행, 입력 길이/프롬프트 길이 변화, `max_cache_len` 고정 상한·재추적·메모리 제한을 설계한 뒤에만 격리 실험한다. 현재 설치된 Transformers는 decode 컴파일과 prefill을 구분하지만 앱은 이 StaticCache 경로를 선택하지 않는다. cold/warm·새 프로세스·미경험 길이·언어 변경·HY 동시 사용까지 통과해야 한다. **같은 B3 코드를 다시 켜거나 긴 준비 비용을 숨기는 시험은 하지 않는다.**

#### CR5 — Whisper를 사용할 때 계산 형식만 비교

근거: [Whisper-Streaming 작성자의 backend 코드](https://github.com/ufal/whisper_streaming/blob/main/whisper_online.py#L108-L121), [CTranslate2 관련 개발자 토론](https://github.com/openai/whisper/discussions/937). NVIDIA L40의 제한적인 경험으로 INT8 혼합 계산이 FP16보다 느렸고 전사도 달랐다는 주석이 있다. 이 장치의 약20% 차이를 RTX 4080의 예상 개선율로 쓰지 않는다.

현재 `runtime.py`는 Whisper CUDA 계산 형식을 `int8_float16`으로 고정한다. 동일 모델·beam 5·단어 시각·VAD를 유지하며 `float16`만 비교하는 후보는 기존 B6와 다르다. 실제 저장 가중치 형식부터 확인해야 하며, INT8 저장 가중치를 FP16으로 실행한다고 원본 FP16 정밀도가 되살아나는 것은 아니다.

Whisper ASR 단독 시간뿐 아니라 HY 동시 사용의 VRAM, 첫 준비와 전체 자막 지연, 중국어·영어·일본어 원문 오류를 비교한다. 메모리가 늘어 번역이나 전사가 느려지면 불채택한다. Qwen 기본 경로에는 영향이 없으므로 현재 주력 조합의 우선순위에 넣지 않는다.

### 11.3 기존 후보와 비교해 제외·후순위로 보낸 것

| 제안 | 이번 판단과 이유 |
|---|---|
| LocalAgreement, 수집/추론 분리, 조기 인식, 예열, HTTP 재사용, 두 줄 표시 예약 | LocalAgreement는 Qwen 안정 확정 옵션, 수집/추론 분리·조기 인식은 Qwen 빠른 경로, 예열·HTTP 재사용·예약은 해당 공통 경로에 구현. 새 발견에서 제외 |
| 더 짧은 고정 ASR 구간·엄격한 VAD·모든 경계 재확인 | 이미 품질 저하/추가 대기를 확인. 같은 조건 재시험 제외 |
| B5: 100→20/40ms 전송을 최우선 가속으로 적용 | **후순위로 변경.** OpenVoiceStream의 다른 장치·프로토콜에서는 묶음 축소 후 지연이 늘었다. 우리에서도 악화한다는 증명은 아니지만, 전체 지연 개선을 예상할 근거가 부족함. 전달 대기가 실제 병목으로 남았을 때만 비교 |
| B8: 종료 대기를 일괄 단축 / 다른 앱의 adaptive timeout 복사 | 기존 후보 유지하되 바로 적용하지 않음. 일부 adaptive 구현은 2초까지 늘려 현재보다 늦어짐. CR3 끝점 평가와 별개의 변수로만 검토 |
| 자동 LLM ASR 교정, 문맥 재번역 감지 후 추가 번역, 작은 모델→큰 모델 이중 결과 | 추론 횟수·수정 횟수 증가. 현재 속도 조건과 중국어→한국어 효과 근거 부족 |
| 무음에서 Silero 호출 생략·큰 VAD batch·스레드 증설 | 현재도 상태를 유지하며 무음을 공급함. 다중통화 처리량 개선은 단일 방송의 지연 개선이 아님 |
| TEN-VAD를 기본 공개 배포에 포함 | [LICENSE](https://github.com/TEN-framework/ten-vad/blob/main/LICENSE)에 Apache 2.0 외 경쟁·배포 관련 추가 조건이 있음. 보통의 Apache-2.0 의존성처럼 취급하지 않고 이번 배포 후보에서 보류 |
| antirez C/CUDA 엔진으로 즉시 교체 | 현재 CUDA 검증 장치/OS가 다르고 상주 인터페이스·Windows 이식·HY 동시 메모리 검증 필요. 4초 독립 입력에 window 캐시 이득도 미확인. 현재 후보에서 제외 |
| Apple MLX lock/Apple Speech, RK NPU 수치, 대규모 vLLM batch를 그대로 도입 | Windows 단일 GPU 한 방송과 실행 구조가 다름. 장치별 최적화를 공통 최적화로 오인하지 않음 |
| 새 모델을 이유만으로 기존 Nemotron/영어 causal/Qwen 파생 모델 재시험 | 중국어 기준 개선 근거 또는 실제 달라진 모델·가설이 먼저 필요. 영어 WER·첫 partial 지연을 한국어 확정 자막 성능으로 바꾸어 설명하지 않음 |

Whisper를 실제로 사용할 경우 기존 **A1 수집/추론 분리**, Whisper/Gemini의 **A2 오래된 잠정 번역 재시도 생략**은 여전히 가치 있다. 이번 공개 구현에서도 수신과 계산 분리·대체된 잠정 요청을 계산 전 제외하는 방향이 확인됐지만 이미 알려진 후보이므로 CR 항목을 새로 만들지 않았다. Gemini에서 확정문 대기 중 잠정 번역 억제가 빠진 차이도 A2와 별도 변수로 기록했다. 현재 Qwen 기본 경로 가속으로 세지 않는다.

### 11.4 다음에 실제 시험한다면

권장 순서: **CR1 고정 원문 비교 → CR2 경계 후보 비교 → CR3 감지 결과 비교와 실제 ASR 확인**. CR4는 앞의 작은 개선과 분리한 조건부 연구다. 앞서 제시했던 ‘전송 간격 축소부터’ 순서는 이번 자료 대조에 따라 갱신한다.

각 시험은 현재 기준선과 최소 3회 AB/BA로 비교한다. 모델·힌트·언어를 동시에 바꾸지 않는다. 기존 영상/SRT는 회귀 기준으로 사용하되 짧은 응답·실제 반복·부정·BGM 경계 표본을 추가한다. **원문/한국어 의미 보존, 추가 불필요 호출 없음, p50/p95/최악 지연 비악화**가 공통 통과 조건이다. 준비 시간·CPU·VRAM도 별도로 기록한다.

새 실험은 CR ID, 소스 revision, 과거와 달라진 변수, 원시 결과, 채택/보류 이유를 §10 양식으로 추가한다. 이번 조사 결과를 ‘효과 확인’이나 ‘수정 적용 완료’로 바꾸어 기록하지 않는다.

## 12. 커뮤니티 후보 실제 구현·비교 결과 — 2026-10-05

**최종 상태: CR1·CR2·CR3은 격리 시험까지 완료했으나 제품에 채택하지 않았다. Whisper FP16도 비교 후 기존 `int8_float16`을 유지한다. Qwen StaticCache·decoder compile은 사용자 지시로 제외했다.** 개선 수치 하나만 보고 기존 앱을 바꾸지 않았으며, 실행 중인 앱에 패치를 적용하거나 실행본을 다시 빌드하지 않았다.

### 12.1 공통 기준과 실제 시험 범위

- 기준 소스는 이 작업 시작 시점의 [엔진 원본 보관본](../../work/community-implementation-20261005/baseline/manifest.json)이다. 이전 B1 HTTP 재사용, A3 표시 예약 및 기존 Qwen 빠른 경로를 유지했다. 큰 Qwen/HY/Whisper 가중치는 복사하지 않고 기존 모델을 참조했다.
- RTX 4080 16GB에서 Qwen3-ASR-1.7B + HY-MT2-7B-Q6_K, 중국어 고정, 힌트 없음, legacy 빠른 경로, 문장 경계 재확인 off. 후보들을 서로 섞지 않았다. GPU 시험은 동시에 실행하지 않았다.
- `ndhgOQNXx9M`의 기존 99.822625초 PCM과 검수 SRT를 사용했다. PCM SHA256 `7dca4122126bcf30709b24cd8690c1d62dbdf95e03976722822f009b2f072f55`, SRT SHA256 `2e23a08b1e96461604f455c688881a2f4d8b958dc5c54b83201999ed80317109`. SRT 원문을 ASR/번역 프롬프트에 주입하지 않았다.
- **지연은 엔진 caption 로그 시각 기준**이며 화면 실측이 아니다. 공통 cue 지연은 정확히 매칭되는 같은 SRT 문장의 끝 시각을 기준으로 한다. SRT 끝은 실제 발화 종료 정답과 다를 수 있다. cue 1의 33ms 문장은 기존 정책대로 CER 점수에서 제외했다. 영어·일본어 품질을 검증한 시험이 아니다.
- 실제 영상 엔진 재생은 기준 3회, CR2 3회, CR3 3회로 총 9회다. CR2는 AB/BA/AB 순서이며, CR3는 같은 코드의 앞선 기준 3회를 재사용하고 후보 3회를 추가했다. **CR3를 새 기준선과 번갈아 실행한 ABBA 시험으로 설명하지 않는다.**

| 항목 | 결과와 결정 |
|---|---|
| CR1 짧은 원문의 HY 문맥 제한 | 732회 번역에서 목표인 배경 재번역 오류가 재현되지 않았고 실제 영상의 영향 문장에 속도/의미 개선 없음. **기존 최대 3문장 유지** |
| CR2 4초 지점에서 과거 짧은 쉼으로 분할 이동 | 구현·CPU 회귀·실제 영상 각 3회 완료. CER 감소는 기준 SRT 밖 후반 문자열 차이이며 본문 오류 수는 그대로. **기존 분할 유지** |
| CR3 FireRed Stream-VAD CPU 힌트 | 공식 소스/가중치·연속 특징·ONNX 경로를 구현하고 실제 영상 3회 완료. 일부 경계 중복은 줄지만 새 인명 누락과 의미 변화, 지연 증가. **기존 Silero 힌트 유지** |
| CR4 Qwen StaticCache·decoder compile | **사용자 제외 지시. 실행·설치·다운로드 없음** |
| CR5 Whisper FP16 | 기존 `model.bin` 자체가 FP16 저장 형식으로 **추가 다운로드 불필요**. 계산 형식별 실제 24회 비교에서 일관된 방송 중 이득 없음. **INT8_FLOAT16 유지** |

### 12.2 CR1 — 문맥 0/1문장 제한, 미채택

현재 제품의 `Runtime.translate`와 HY 템플릿/샘플링을 그대로 사용했다. 기존 영상의 실제 ASR 원문+문맥 35개와 중국어 대조군 26개, 총 61개를 사전에 고정했다. 짧은 단독 표현 18개에만 문맥 제한을 적용하고 인명/대명사/부정/수량은 유지했다. 각 후보(0문장/1문장)에 기준·후보 각 3회, 총 732번 실제 번역 요청과 별도 예열 1회. 오류·재시도는 없었다.

실제 영상에서 규칙의 영향을 받는 것은 `啊。`, `对吧？` 두 문장뿐이며 번역은 모두 동일했다. 이 6쌍의 후보−기준 처리시간 차이 중앙값은 문맥 0에서 **+7.460ms**, 문맥 1에서 **+0.496ms**였다. 전체 영상 집단의 일부 작은 감소는 문맥이 바뀌지 않은 제어군에도 나타났으며 prompt cache 조건의 차이가 있어 효과로 세지 않았다. 합성 짧은 문장에서 말투·담화 기능 변화는 있었지만, 배경 재번역 문제의 해결로 볼 수 없었다.

같은 61문장과 동일 0/1 규칙은 다시 시험하지 않는다. 현재 프로필에서 배경 자체를 번역하는 실제 원문/문맥/출력 사례가 확보되거나 다른 가설이 있을 때만 재검토한다. [CR1 상세 결과](../../work/community-implementation-20261005/cr1/CR1_결과.txt), [판정](../../work/community-implementation-20261005/cr1/decision.json), [원시 요청](../../work/community-implementation-20261005/cr1/run-1/records.jsonl).

### 12.3 CR2 — 최근 쉼으로 4초 분할 이동, 미채택

4초 상한에 도달했을 때 최근 600ms 안의 완료된 80ms 이상 endpoint quiet 구간 끝으로만 경계를 당겼다. 미래 입력 대기와 추가 모델 호출은 없고, 경계 뒤 모든 PCM과 기존 200ms overlap을 다음 창으로 넘겼다. in-flight/cached 인식은 변경된 경계에 재사용하지 않도록 revision을 갱신했다.

CPU 검토 중 **자른 앞 구간의 새 음성 길이가 최소 200ms 아래로 떨어져 통째로 버려질 수 있는 후보 버그**를 찾아 수정했다. 9개의 짧은 음성 뒤 긴 발화가 이어지는 반례를 추가하여, 경계 이동 후 앞 구간도 최소 조건을 만족할 때만 이동하도록 했다. 이 오류는 시험 후보에서 발견했으며 제품에 들어간 적 없다. 샘플 보존·짧은 꼬리·분할 불변성·중복 범위·오래된 잠정 결과 차단을 포함하여 [후보 CPU 검사](../../work/community-implementation-20261005/cr2/cpu-tests.log)는 **1022 passed, 1 skipped**였다. 최초 전체 검사 수집 시 격리본에 tests helper가 없어 발생한 경로 오류는 시험 import 경로를 보완한 뒤 재실행했다.

실제 영상에서 2곳의 경계만 바뀌었다. 3회씩 같은 조건 안의 원문은 동일했고, 기준 35 ASR/35 MT → 후보 35 ASR/33 MT였다. CER **15.8845%→14.8014%**의 3편집 감소는 모두 SRT에 없는 마지막 브랜드 문자열의 삽입 12→9 감소다. 단독 `啊`가 사라진 본문 cue는 치환 1개가 삭제 1개로 바뀐 것으로, 본문 오류 수 감소가 아니다. SRT 밖 원문을 환각으로 단정하거나 실제 대사 개선으로 세지 않았다.

첫 한국어 자막 중앙값 **5.110→5.125초**. 공통 17 cue×3회, 같은 반복끼리 51쌍의 후보−기준 지연 차이는 중앙값 **0ms**, 평균 **+2.686ms**, p95 **+78ms**였다. 실익이 입증되지 않아 제품 패치를 만들지 않았다. [CR2 결과](../../work/community-implementation-20261005/cr2/CR2_결과.txt), [의미 검토](../../work/community-implementation-20261005/cr2/semantic-review.txt), [전체 비교](../../work/community-implementation-20261005/cr2/comparison.json), [후보/재시도 조건](../../work/community-implementation-20261005/cr2/decision.json).

### 12.4 CR3 — FireRed Stream-VAD, 미채택

공식 소스 `c30ec49e8cc69642b0ee65362eba11b9d11c6e54`, HF Stream-VAD 가중치 `7990aaccc6b7aec1e527743bd30201f2c4a03b8c`를 work에 내려받았다. 약 2.2MB, 567,937 parameters, N2=0을 확인했다. 기존 대형 가중치를 복제한 것이 아니다. [출처/해시](../../work/community-implementation-20261005/cr3/source-manifest.json).

25ms 특징 창/10ms hop의 연속 fbank와 DFSMN cache를 유지했다. 실제 후보는 공식 cached ONNX + CPU ORT 세션 1thread, 과거 5프레임 평균, 기존 0.5/0.35 hysteresis다. 전역 Torch thread/CUDA 환경은 바꾸지 않았다. RMS 시작, PCM 소유권, 4초 상한, 기존 500ms 종료 및 전체파형 Silero gate는 그대로이며 FireRed 자체 종료 대기를 직렬 추가하지 않았다. 실제 후보에서는 프레임 로그/소요시간 목록을 누적하지 않는다.

청크 분할·reset·EOF 특징 수 검사를 통과했다. ONNX↔Torch 확률 최대 차이는 약 `7.45e-7`, 같은 영상의 bool 판정과 분할은 동일했다. CPU 호출 p95는 기존 Silero **0.105~0.107ms**, FireRed ONNX **1.171~1.424ms**. Torch 순차 경로보다 줄었지만 기존보다 CPU 비용이 높았다. [CPU 결과·한계](../../work/community-implementation-20261005/cr3/결과_요약.txt), [ONNX 비교](../../work/community-implementation-20261005/cr3/onnx-results/summary.json).

실제 Qwen+HY 재생 3회에서 CER은 모두 **13.7184%**(기준 15.8845%). 그러나 6편집 감소 중 3개는 SRT 밖 광고 꼬리 차이이며, 본문에서도 개선과 퇴보가 동시에 있었다.

- `欺负的`, `就是` 등의 경계 중복 일부가 줄어 문장이 연결되는 개선이 있었다.
- 새 분절에서 `顾小棠` 인명이 통째로 빠졌다. **68.08~70.02초의 인명 창이 기존 전체파형 Silero gate에서 `[]`로 거절**되어 Qwen까지 가지 않았고, 뒤 `我做鬼…`만 인식됐다. 기존 68.10~72.10초 창은 통과했다. PCM을 저장·이월하는 것만으로 downstream 필터의 누락까지 방지되는 것은 아니다.
- `为我赚钱啊`가 `为我赚钱了`로 바뀌면서 한국어에 ‘돈을 벌었군’처럼 과거 의미가 추가됐다. 문자 오류율 하나로 번역 의미의 비악화를 보장할 수 없다.

세 번 모두 ASR 호출 **35→38**, MT/확정 자막 **35→32**. 같은 공통 12 cue×3의 로그 기준 지연은 p50 **1.107→1.242초**, p95 **3.380→3.745초**, 최대 **3.472→4.050초**. 첫 자막 중앙값은 **5.110→5.141초**다. 새로운 인명 누락과 지연 증가가 사용자 조건을 충족하지 않아 제품에 넣지 않았다. [실제 3회 비교](../../work/community-implementation-20261005/cr3/comparison.json), [상세 의미/필터 상호작용](../../work/community-implementation-20261005/cr3/결과_요약.txt).

공식 코드·모델 라이선스와 별개로, 격리 사용한 kaldiio wheel의 동봉 LICENSE에 평가용 조건을 확인했다. 앱에 설치·배포하지 않았다. 채택 가능성이 생길 경우 이를 제외할 수 있도록 strict CMVN reader를 별도 준비했으며, 9980×80 특징 정규화가 기존과 완전히 일치했다. 이 대안도 **미적용**이다. [대안·배포 조건](../../work/community-implementation-20261005/cr3/licensing-alternative/설명.txt).

같은 smooth5 후보를 다시 기본값으로 바꾸지 않는다. 재검토한다면 새 분절과 전체파형 gate의 충돌을 먼저 해결하고 인명/짧은 발화·BGM 회귀 및 실제 지연을 새 가설로 검증해야 한다. whole-wave gate를 무조건 제거하거나 VAD threshold를 이 영상에만 맞춰 조정한 시험은 하지 않았다. raw probability 후보는 CPU shadow만 비교했으며 실제 Qwen+HY 검증을 했다고 설명하지 않는다.

### 12.5 CR5 — Whisper 저장 형식 확인과 FP16 비교

현재 `models/asr/whisper-large-v3-turbo/model.bin`은 CT2 v6/WhisperSpec rev3이며 **float16 텐서 475개**를 담고 있다. 작은 int8/int16 항목은 구조 플래그이고 양자화 scale 텐서가 없다. 486개 텐서와 alias를 읽은 끝이 파일 크기 1,617,884,929 bytes와 일치했다. **새 모델 다운로드·변환 없이 동일 파일의 runtime 계산 형식만 바꿔 비교할 수 있다.** [저장 형식 검사](../../work/community-implementation-20261005/whisper/model-storage.json).

별도 프로세스에서 INT8_FLOAT16→FP16 순서로 1/2/4/8초를 포함한 중국어 입력 8개×3회씩, 총 48회 실제 인식을 했다. 실제 CT2 compute_type도 요청값과 일치했다. beam 5·temperature 0·VAD 500ms·단어 시각·후처리를 유지했다. 첫 순회와 반복 순회를 구분했다.

반복 16호출씩의 중앙값은 **161.98→161.17ms**, p95는 **260.18→266.77ms**였다. 1초 입력에서 FP16 출력이 길게 나오던 문장에서 한 글자 `請`로 바뀌어 빨라진 효과가 섞여 있다. 1초 입력 제외 평균은 **179.08→181.87ms**, 같은 원문이 나온 4입력 평균은 **163.59→164.68ms**로 오히려 조금 느렸다. 8입력 중 4개는 정규화 후에도 전사 내용이 달랐다. 부분적으로 겹치는 SRT를 음성 crop 전체의 정답으로 삼지 않아 정확도/CER 우열을 단정하지 않았다.

한 번의 프로세스별 초기 load/예열 수치는 캐시·순서 조건이 달라 시작 속도 개선으로 일반화하지 않는다. 반복 처리의 뚜렷한 이득이 없어 **HY 동시 전체 경로·peak VRAM 시험으로 확대하지 않았으며** 기존 INT8_FLOAT16을 유지한다. [Whisper 비교 결과](../../work/community-implementation-20261005/whisper/비교_결과.txt), [수치·원문](../../work/community-implementation-20261005/whisper/precision-analysis.json).

### 12.6 최종 반영·보관 상태

- 제품 엔진 61개 파일은 시험 전 보관본과 SHA256가 같고, 9회 영상 시험의 설정·자막 기록·기준 SRT 보호 검사도 모두 통과했다. CR3의 외부 연구 의존 파일 131개도 각 실제 시험 전후 동일했다. [보존 검증](../../work/community-implementation-20261005/preservation.json).
- 앱 실행 코드·기본값·모델·번역 기록은 유지했다. 실행본 재빌드·배포본 복사·E: 작업은 하지 않았다. 이번 제품 폴더 변경은 이 기록 문서뿐이다. 시험용으로 띄운 모델 프로세스는 종료했다.
- `work/community-implementation-20261005`의 후보 소스·runner·원시 로그·분석·작은 FireRed 자료는 재현 근거다. 미채택 코드를 제품 소스와 혼동하거나 이 연구 폴더를 앱 배포에 통째로 포함하지 않는다. 기존 0_사용법과 별도의 사용법은 만들지 않았다.
- 이번 결과는 한 중국어 영상과 짧은 번역 대조군에 대한 결과다. 기존 앱의 남은 오류가 해결되었다거나 다른 언어/기기/새 모델에서도 후보가 항상 불리하다는 뜻은 아니다. 재시도는 각 항목의 실패 원인·달라진 조건을 먼저 확인한 뒤 결정한다.

## 13. 공개 ASR 비교와 후속 검증 우선순위 재검토 — 2026-10-05

**이번 범위는 공개 자료 확인과 기존 시험 결과 해석이다. 새 ASR 모델 다운로드·실행, 추가 GPU 시험, 제품 코드·설정 변경은 하지 않았다. 최종 판단은 현재 Qwen3-ASR-1.7B + HY-MT2 조합 유지, 새 ASR 비교 시험과 CR3 필터 충돌 추가 검증 보류다.** §11의 시험 전 권장 순서보다 §12의 실제 결과와 이 절의 후속 판단을 우선한다.

### 13.1 공개 기술의 한계인지에 대한 판단

- 이번 결과만으로 공개 ASR 기술 전체가 한계에 도달했다고 결론 내릴 수 없다. 지금까지의 실제 검증은 주로 한 중국어 영상이며, 현재 조합의 작은 설정 변경에서 얻을 이득이 줄어든 상태로 해석한다.
- 현재 목표는 **지연을 늘리지 않으면서 체감할 수 있는 인식 개선**이다. 공개 평가에서 오류율이 조금 낮거나 모델 크기가 작다는 사실만으로 통합·교체 시험의 우선순위를 높이지 않는다.
- 현재 Qwen 빠른 경로의 음성 수집·분절 대기와 ASR 계산 시간은 별개다. 다른 모델의 대량 처리 속도가 빨라도 확정 한국어 자막까지의 대기가 줄어든다는 보장은 없다.
- 현재 확인한 범위에서는 속도를 유지하면서 인식률을 눈에 띄게 높일 확실한 대안을 찾지 못했다. 개선 불가능의 증명도, 새 후보의 개선 효과 확인도 아니다.

### 13.2 Qwen 대비 Fun-ASR-Nano·FireRedASR2 공개 평가

FireRed 개발팀이 공개한 같은 평가표의 **문자 오류율(CER, %, 낮을수록 좋음)**이다. 중국어 평가 세트별 동일 가중치 평균이며 기본적으로 비스트리밍 조건이다. 독립 제3자 평가나 우리 영상·Windows 앱의 실측값으로 소개하지 않는다.

| 모델 | 표준 중국어 4개 평균 | 중국 방언 19개 평균 | 전체 24개 평균 | 이번 판단 |
|---|---:|---:|---:|---|
| Qwen3-ASR-1.7B — 현재 주력 | 3.76 | 11.85 | 10.12 | 현재 기준선 유지 |
| Fun-ASR-Nano-2512 | 4.55 | 15.07 | 12.81 | 이 평가에서는 Qwen보다 불리함. 정확도 개선용 교체 우선순위 낮음 |
| FireRedASR2-AED | 3.05 | 11.67 | 9.80 | 중국어 개선 가능성은 있으나 실제 앱의 속도·짧은 구간 품질 미확인 |
| FireRedASR2-LLM | 2.89 | 11.55 | 9.67 | 평균 CER은 낮지만 모델 규모와 동시 실행 부담 때문에 후순위 |

출처: [FireRedASR2S 논문 §7.1·부록 A/Table 6](https://arxiv.org/html/2603.10420v1#A1), [Qwen 비교 모델을 1.7B로 명시한 공식 평가](https://github.com/FireRedTeam/FireRedASR2S#evaluation). 전체 24개는 표준 중국어 4개·방언 19개·가창 1개다. 일부 광둥어 대화에서는 Qwen이 FireRed보다 낮은 CER을 보였으므로 모든 중국어 상황의 우열로 일반화하지 않는다.

언어·모델·실행 조건은 다음과 같이 구분한다.

- **Qwen3-ASR-1.7B:** 중국어·영어·일본어를 포함한 다국어 지원. 현재 앱에서 실제 검증한 주력이다. [공식 Qwen 자료](https://github.com/QwenLM/Qwen3-ASR).
- **Fun-ASR-Nano:** 공식 표기 약 0.8B, 중국어·영어·일본어 지원. 별도 모델인 `Fun-ASR-MLT-Nano`의 평가 수치를 일반 Nano의 수치로 쓰지 않는다. 공식 표에 있는 비공개 Fun-ASR 7.7B/API 성능도 다운로드 가능한 Nano의 성능과 구분한다. [공식 Nano 모델 카드](https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512), [공식 실행 안내](https://github.com/QwenAudio/Fun-ASR).
- **FireRedASR2:** AED는 공식 표기 1B+, LLM은 8B+. 중국어·중국 방언·영어·중영 혼합을 지원하며 일본어 ASR은 지원 목록에 없다. VAD/LID의 100개 이상 언어 지원을 ASR 지원 언어로 바꾸어 설명하지 않는다. 공식 코드는 Ubuntu 22.04에서 시험했고 Windows는 미검증이다. RTX 4080 16GB에서 특히 LLM판과 HY 번역 모델의 동시 메모리·속도 조건은 확인하지 않았다. [공식 모델·사용 안내](https://github.com/FireRedTeam/FireRedASR2S), [모델 구조·규모](https://arxiv.org/html/2603.10420v1#S3).
- **속도 순위 미확인:** FireRed의 12.7배 가속은 H20에서 같은 AED의 PyTorch 대비 TensorRT 대량 처리 결과다. Qwen 대비 속도나 첫 자막 지연을 뜻하지 않는다. Nano의 H100 배치 처리량도 우리 RTX 4080 단일 방송과 조건이 다르다. 이 수치로 교체 후 빨라진다고 예고하지 않는다. [FireRed 가속 조건](https://github.com/FireRedTeam/FireRedASR2S/tree/main/runtime/triton_tensorrt), [Nano 배치 처리 조건](https://github.com/QwenAudio/Fun-ASR#faster-batch-transcription-no-vllm).

**최종 우선순위:** 처음에는 FireRedASR2-AED를 중국어 비교 후보로 제시했으나, 표준 중국어 CER 차이 0.71%p와 실제 속도 근거 부족, 통합 비용을 함께 고려해 **시험 보류**로 낮췄다. 의미가 전혀 없는 차이라고 단정하지는 않지만 현재 요구에 맞는 교체 이득은 입증되지 않았다. Fun-ASR-Nano도 정확도 개선 목적의 시험을 보류한다. 이 모델들은 이번에 실제 실행·비교한 적이 없다.

### 13.3 ‘필터 간 충돌을 좁혀 검증’의 정확한 의미와 적용 범위

여기서 필터는 번역 내용이나 성적 표현을 거르는 필터가 아니라 **사람 말과 비음성을 구분하는 VAD**다. §12.4의 미채택 CR3 실험판에서 다음 경로가 확인됐다.

1. 앞단 FireRed 종료 신호가 68.08~70.02초의 인물 이름 부분을 별도 음성 구간으로 닫았다.
2. 기존 후단 전체파형 Silero gate가 그 짧은 구간을 `[]`(음성 없음)로 판정했다.
3. 해당 구간이 Qwen 추론에 전달되지 않아 이름이 통째로 빠졌다. 뒤 대사는 별도로 인식됐다.
4. **현재 기본 방식의 68.10~72.10초 구간은 같은 후단 gate를 통과해 이름을 포함했다.** 이름의 글자 정확도와 이름 전체 누락 여부는 별개의 문제다.

근거: [CR3 의미 검토·필터 재현](../../work/community-implementation-20261005/cr3/결과_요약.txt), [판정과 재개 조건](../../work/community-implementation-20261005/cr3/decision.json). FireRed Stream-VAD의 이 결과를 FireRedASR2의 인식 성능 실험으로 설명하지 않는다.

‘좁혀 검증’은 해당 조합에서 짧게 분리된 발화의 후단 판정을 조정할 구체적인 가설을 세우고, 이름·짧은 말 보존과 함께 음악/효과음 오인식·추가 ASR 호출·전체 지연을 비교한다는 뜻이었다. 전체 필터 제거, 모든 임계값 완화, 지연 개선 보장을 뜻하지 않는다.

**설명 정정:** 이 충돌을 현재 제품에서 확인된 필수 수정 사항처럼 설명해서는 안 된다. 확인된 범위는 미채택 FireRed 실험판이며, 현재 기본판은 해당 구간을 처리했다. 다른 모든 구간에서도 후단 필터에 의한 누락이 없다고 증명한 것은 아니다.

**최종 결정: 추가 검증 보류.** 충돌을 해결해도 CR3에서 함께 발생한 지연 증가와 다른 의미 변화가 해결된다는 근거가 없다. 현재 앱 개선의 우선 작업으로 잡지 않으며, FireRed 종료 방식을 다시 도입할 구체적인 이유가 생길 때 검토한다.

### 13.4 이후 작업에서 유지할 결정과 재개 조건

| 항목 | 현재 결정 | 다시 검토할 근거 |
|---|---|---|
| 기본 ASR·번역 조합 | Qwen3-ASR-1.7B + HY-MT2 유지 | 현재판에서 반복되는 실제 오류, 달라진 모델/런타임, 요구 조건 변경 |
| Fun-ASR-Nano·FireRedASR2 비교 | 자료 검토 완료, 실행 시험 보류 | 짧은 중국어 구간에서 인식 개선과 지연 비악화를 기대할 구체 자료 또는 사용자 재시험 지시 |
| CR3 필터 충돌 검증 | 보류, 현 제품 패치 대상 아님 | FireRed 종료 방식 재도입 필요 또는 현재 기본판에서도 재현되는 동일 누락 |
| 기존 실패 후보 반복 | 같은 조건으로 반복하지 않음 | 실패 원인을 겨냥한 새 가설·변수·표본을 먼저 명시 |
| Qwen StaticCache·decoder compile | 사용자 지시로 제외 유지 | 사용자 지시 변경 전에는 시험 목록에 다시 넣지 않음 |

현재판에서 반복 누락·오인식이 발견되면 원본 음성 구간과 로그로 **음성 수집/분절 → 후단 gate → ASR 원문 → 한국어 번역** 중 실제 발생 단계를 확인한다. 확인되지 않은 처리 문제를 만들어 개선 과제로 삼지 않는다. 이 기록 자체는 새 시험 시작 지시가 아니다. 앱 실행 코드·모델·기본값·최근 번역 기록은 변경하지 않았고 E드라이브 복사·배포본 생성도 하지 않았다.

## 14. 현재 구현 기술의 심층 자료 대조 — 2026-10-05

후속 상태: 이 절은 조사 당시의 미검증 후보 기록이다. 이후 사용자 지시로 D1·D2·D3를 실제 실행한 결과와 최종 미채택 판단은 **§15**를 따른다.

**사용자 요청에 따라 현재 코드·동봉 라이브러리·공식 논문·저자 소스·중국어권/영어권 개발자 이슈를 더 깊게 대조했다. 큰 구조를 바꾸면 즉시 더 빨라진다는 새 근거는 찾지 못했다. 아래 후보는 실제 모델 품질·속도 개선이 입증된 수정이 아니다. §12·13의 미채택/보류와 사용자 제외 항목은 유지한다.**

이번에는 제품 코드·기본값을 수정하거나 모델을 새로 내려받지 않았다. 새 ASR/번역 GPU 시험도 하지 않았다. 확인한 수치의 범위는 과거 profiler 로그 재집계와 고정 가상 로짓을 사용한 CPU 연산 예제다. CPU 예제는 실제 한국어 번역 시험이나 llama 바이너리 검증이 아니다. [검토 소스 15개 해시](../../work/deep-implementation-research-20261005/inspected-source-manifest.json)는 관측 시점 식별용이며 시험 전후 보존 검사를 대신하지 않는다.

### 14.1 현재 구현에서 이미 충족한 부분

| 대조 항목 | 확인과 판단 |
|---|---|
| Qwen 입력 패딩 | Transformers 5.18 native processor는 단건 길이에 맞춘 padding과 모델이 요구하는 최소/블록 정렬을 사용한다. 모든 4초 입력을 30초로 패딩한다는 가정은 틀림 |
| Qwen 생성 캐시 | 한 번의 생성 안에서 decoder KV cache를 쓰고, 음성 encoder는 최초 prefill에서 수행하며 마지막 필요한 위치의 logits를 계산함. ‘캐시 켜기’를 새 최적화로 세지 않음 |
| 무음 중 사전 인식 | 같은 음성 revision의 유효한 결과를 재사용하면 Runtime/VAD/전처리/추론을 건너뜀. 새 발화로 무효화하는 정책도 이미 있음 |
| Silero 상태·시간 | 스트림별 h/c와 64샘플 문맥, 512샘플 순차 공급, 별도 PCM 시계를 유지하고 무음도 모델에 공급함. 주기적 reset이나 무음 입력 생략을 새 기본값으로 넣을 근거 없음 |
| 공식 Qwen streaming | 누적 음성을 다시 입력하고 출력 뒤 토큰을 되돌리는 wrapper 구조. 음성 encoder가 단순 suffix만 처리하는 기능으로 해석하거나 현재 4초 독립 창의 무비용 교체재로 소개하지 않음 |
| 최근 Qwen 이슈 | 원본 qwen-asr 0.0.6의 SDPA 창/다른 길이 batch 이슈를 현재 native Transformers 5.18 단건 경로의 버그로 간주하지 않음 |
| 번역 HTTP·prefix cache | HTTP 재사용 B1은 이미 적용됐고, 조사한 llama b11378의 `cache_prompt` 기본값은 true다. 요청에서 생략했다는 이유로 캐시 미사용이라고 단정하지 않음 |
| 번역 큐·두 줄 표시 | 동일 ID의 낡은 수정만 정리하고 서로 다른 확정문은 보존한다. 두 줄 읽기 시간과 다음 교체 예약 A3는 이미 구현. 표시 대기를 무조건 버그로 보아 삭제하지 않음 |

근거: [Qwen 세부 코드·이슈 대조](../../work/deep-implementation-research-20261005/qwen-research.txt), [native processor v5.18.0](https://github.com/huggingface/transformers/blob/v5.18.0/src/transformers/models/qwen3_asr/processing_qwen3_asr.py), [native 모델 v5.18.0](https://github.com/huggingface/transformers/blob/v5.18.0/src/transformers/models/qwen3_asr/modeling_qwen3_asr.py), [Qwen 공식 streaming 소스](https://github.com/QwenLM/Qwen3-ASR/blob/main/qwen_asr/inference/qwen3_asr.py), [Silero 유지관리자 논의](https://github.com/snakers4/silero-vad/discussions/804).

### 14.2 새로 구분한 조건부 검토 후보

**D1 — HY 숫자 설정과 실제 샘플링 연산 순서의 차이(세부 보고서 T1)**

현재 앱은 HY에 `temperature=0.7`, `top_k=20`, `top_p=0.6`, `repeat_penalty=1.05`, `min_p=0`을 보내지만 sampler 순서는 지정하지 않는다. 조사한 llama.cpp b11378의 기본 유효 순서는 penalties → top_k → top_p → temperature이며, Transformers 5.18의 순서는 repetition penalty → temperature → top_k → top_p다. **권장 숫자가 같아도 같은 후보 분포가 되는 것은 아니다.** 이를 현재 번역의 오역 원인이 확인됐다고 해석하지 않는다.

모델 없는 CPU 예제에서 같은 가상 로짓과 같은 수치를 사용하되 순서만 바꾸면 후보가 `[0, 1]`에서 `[0]`으로 달라졌다. 두 순서를 설치 Transformers 연산자로 표현한 수학적 재현이며 실제 HY/llama 출력은 측정하지 않았다. [재현 코드](../../work/deep-implementation-research-20261005/sampling_order_cpu.py), [CPU 결과](../../work/deep-implementation-research-20261005/sampling-order-cpu.json).

llama의 기본 반복 패널티 범위 64토큰과 Transformers의 입력 전체 범위 차이도 별도 변수다. sampler 순서와 범위를 한꺼번에 바꾸면 원인을 구분할 수 없다. 향후 비교할 경우 현재 7B Q6_K·문맥·프롬프트·EOS 보정을 유지하고 한 변수씩, 실제 고정 원문의 의미·인명/부정/수량·한국어 출력·반복·지연을 비교해야 한다. 새 모델이나 추가 번역 단계를 요구하지 않는 제한된 후보지만 생성 길이와 내용이 달라질 수 있으므로 속도 비악화를 미리 보장하지 않는다. **실제 번역 비교·채택은 아직 하지 않았다.**

출처: [HY-MT2 공식 권장값](https://raw.githubusercontent.com/Tencent-Hunyuan/Hy-MT2/main/README.md), [7B 공식 모델 카드](https://huggingface.co/tencent/Hy-MT2-7B), [llama b11378 sampling 기본값](https://raw.githubusercontent.com/ggml-org/llama.cpp/b11378/common/common.h), [Transformers v5.18 생성 순서](https://github.com/huggingface/transformers/blob/v5.18.0/src/transformers/generation/utils.py). 공식 HY 문서의 모델 세대·예제와 현재 앱의 양자화 파일을 동일 실행으로 취급하지 않는다. 현재 raw README의 공통 Transformers 예제는 30B이며 별도 7B 카드와 구분했다. HY main의 고정 SHA는 확보하지 못했으므로 관측일 기준 자료로 기록한다. [번역·큐·표시 세부 대조](../../work/deep-implementation-research-20261005/translation-display-research.txt).

**D2 — Qwen 정상 입력 검사에서의 작은 CUDA 동기화 비용, 낮은 우선순위**

native 모델의 음성 토큰 수 검사는 정상 입력에도 CUDA 스칼라가 포함된 오류 설명 문자열을 먼저 만든다. PyTorch 0차원 Tensor의 문자열 변환이 `.item()` 경로를 사용하는 점을 소스로 확인했다. 이는 기존 B2의 층별 `.tolist()` 재사용과 다른 위치지만, prefill의 작은 비용이어서 체감 개선 가능성을 높게 평가하지 않는다.

기존 4초 profiler의 전체 `.item()` 시간에는 생성 종료 확인 등 다른 호출과 중첩 시간이 섞여 있다. 그 합을 해당 한 행의 비용이나 제거 가능한 시간으로 쓰지 않는다. 같은 자료에서 Hann window 생성은 약 119µs 수준이어서 전처리 캐시나 GPU 특징 추출을 큰 개선안으로 제시할 근거도 부족하다. `audio_kwargs.device='cuda'`만 바꾸면 현재 extractor의 CPU 반환과 앱의 GPU 이동 사이에 왕복이 생길 수 있다.

재검토한다면 입력 검사를 보존한 채 문자열 생성 한 지점의 비용부터 구분해야 한다. 설치 라이브러리의 영구 monkeypatch, 검사 삭제, 넓은 런타임 교체를 권하지 않는다. **새 GPU 측정·패치·효과 수치는 없음.** [코드 위치·기존 trace 근거](../../work/deep-implementation-research-20261005/qwen-research.txt), [PyTorch Tensor 변환](https://github.com/pytorch/pytorch/blob/v2.11.0/torch/_tensor.py).

**D3 — Whisper 시각 토큰 생략과 단어 정렬 유지, Whisper 사용 시에만 조건부**

현재 `without_timestamps`는 기본 `False`, `word_timestamps=True`다. 전자는 디코더가 생성하는 시각 토큰, 후자는 attention/DTW로 얻는 단어 시각으로 별개다. `without_timestamps=True`와 `word_timestamps=True`를 함께 쓰는 후보는 단어 시각을 끄자는 기존 제안과 다르며, 추가 모델을 의도한 것도 아니다.

다만 현재 faster-whisper 1.2.1은 이 조합에서 마지막 단어 끝으로 `seek`를 조정할 수 있다. 줄어든 시각 토큰 계산보다 꼬리 재인식이 늘거나 segment start/end가 달라질 수 있으므로 안전한 가속 옵션으로 단정하지 않는다. 추가 시험을 한다면 generate 호출 수·seek 진행·단어 시각·꼬리 누락/중복·LocalAgreement 관측 횟수·전체 한국어 완료 지연을 함께 비교해야 한다. **이번에는 정적 코드 확인만 했으며, Qwen 가속 후보로 세지 않는다.** Whisper에서 기존 A1/A2보다 높은 우선순위로 올리지 않는다. [Whisper 상세 조사](../../work/deep-implementation-research-20261005/whisper-research.txt), [faster-whisper 고정 소스](https://github.com/SYSTRAN/faster-whisper/blob/65882eee9f5cdbeeb2d877f1131d48cf241b327d/faster_whisper/transcribe.py).

### 14.3 분절 모델 추가보다 평가 해석에서 참고할 점

Smart Turn/LiveKit의 종료 모델은 ‘사람이 말을 마쳤으니 상대가 응답해도 되는가’를 주로 다룬다. 방송 자막의 적절한 번역 단위와 동일한 목표가 아니다. Smart Turn은 VAD 뒤에서 문맥 음성을 평가하며 아주 짧은 독립 음성에 적합하지 않다고 설명한다. 텍스트 기반 종료 모델은 먼저 ASR 원문이 필요하다. 작은 CPU 추론 시간만으로 우리 4초 수집·한국어 표시 대기가 줄어든다고 결론 내리지 않는다. **현재 도입·모델 다운로드·시험 후보에서는 후순위 유지.** [Smart Turn 공식 입력 조건](https://github.com/pipecat-ai/smart-turn#notes-on-input-format), [LiveKit 모델·대기 조건](https://docs.livekit.io/agents/logic/turns/turn-detector/).

새로 참고할 가치가 있는 것은 [Better Late Than Never 논문 v2](https://arxiv.org/html/2509.17349v2)와 저자의 [OmniSTEval](https://github.com/pe-trik/OmniSTEval)이다. 분할·출력 길이에 따라 지연 평가가 치우치는 문제, 분할이 달라진 결과의 재정렬, 빈 출력 보고를 다룬다. 실제 후속 변경을 평가할 때 다음을 보완할 근거로 남긴다.

- 기존 공통 cue 지연은 **매칭된 부분의 지연**이다. 매칭·부분 매칭·누락·정렬 불명확 구간 수를 동일한 전체 참조 분모로 함께 보고한다. CR2의 17개와 CR3의 12개를 같은 평가 집합으로 취급하지 않는다.
- 검수된 중국어 SRT는 한국어 번역 정답이 아니다. 한국어 번역 품질 점수/정렬은 별도 참조가 필요하며, SRT cue 끝도 실제 음절 종료 정답과 다를 수 있다.
- 엔진 로그 시각과 화면 표시 시각을 구분하는 기존 정책을 유지한다. 화면 측정은 재도입하지 않는다. 정렬이 불명확한 시간을 확정 수치로 채우지 않는다.
- 이 보완은 로그 분석 방법이다. 앱 가속 기능도, CR2/CR3 결과를 뒤집는 증거도 아니며 같은 GPU 시험부터 다시 돌릴 필요도 없다.

[LiveKit eot-bench](https://github.com/livekit/eot-bench)의 false cutoff와 endpointing delay 공동 평가도 참고할 수 있다. 다만 인간-에이전트 대화 데이터의 결과가 방송+BGM을 대신하지 않는다. 평가 도구·데이터는 이번에 설치하거나 실행하지 않았다. [분절·평가 상세 조사](../../work/deep-implementation-research-20261005/segmentation-evaluation-research.txt).

### 14.4 공개 앱의 빠른 수치·확정 옵션을 그대로 적용하지 않은 이유

- 중국어 공개 앱 **twojian/WhisperLive**의 README는 첫 글자 150~300ms, 전체 300~600ms를 제시한다. 고정 소스 `6d2864a72156730f5e996448fb50c4e7cdc814c7`에서 확인한 `total_latency`는 제출부터 ASR 결과까지이며 결과 큐·확정·한국어 번역까지 포함하지 않는다. 테스트에는 tiny/합성 입력과 단계별 상수를 더하는 모의 분석이 포함돼 있다. 해당 수치로 현재 앱의 확정 한국어 지연과 비교하지 않는다. [측정 코드](https://github.com/twojian/WhisperLive/blob/6d2864a72156730f5e996448fb50c4e7cdc814c7/recognition/low_latency.py), [테스트](https://github.com/twojian/WhisperLive/blob/6d2864a72156730f5e996448fb50c4e7cdc814c7/tests/test_latency_analysis.py).
- 해당 앱의 유사도 기반 결과 생략을 그대로 가져오면 실제 반복 대사를 버릴 수 있다. 슬라이딩 창·수집/추론 분리·두 단계 VAD 자체는 기존 조사와 겹친다.
- WhisperLiveKit에는 probability > 0.95 단어를 다음 관측과의 일치 없이 확정하는 `confidence_validation` 옵션이 있으나 기본은 꺼져 있다. 이 확률이 중국어 방송의 정답률 95%라는 보장은 없다. 원문 확정이 빨라져도 문장 묶기와 번역까지 빨라지는지 별도 문제여서 보류한다. [고정 소스](https://github.com/QuentinFuxa/WhisperLiveKit/blob/363e4f6d029694d9c81ae548beddd9d3c88a3637/whisperlivekit/local_agreement/online_asr.py).
- `qwen3-asr-stream`처럼 서비스/API에 streaming이라는 이름이 있어도 음성 encoder의 증분 계산을 뜻하지 않는다. partial/final 이벤트·backlog 제한 등은 현재 앱과 겹치는 서비스 책임이다. [중국어권 서비스의 API 계약](https://github.com/LanceLRQ/qwen3-asr-service/blob/main/docs/api/v2/transcription_EN.md).

### 14.5 후속 판단

| 항목 | 이번 확인 수준 | 다음 작업의 가치 |
|---|---|---|
| D1 HY 샘플링 순서 | 런타임 소스 차이 + 모델 없는 CPU 후보 분포 재현 | 새 모델 교체보다 작은 실제 번역 비교 후보. 개선·채택 미확인 |
| D2 Qwen 검증 문자열의 CUDA 스칼라 변환 | 정적 경로 + 기존 profiler 대조 | 낮은 우선순위. 작을 수 있는 비용을 확인하기 위한 항목이며 큰 가속 약속 없음 |
| D3 Whisper 시각 토큰과 단어 정렬 분리 | 정적 구현 확인 | Whisper를 실제 사용할 때만 고려. 기본 Qwen 가속과 무관 |
| 로그 평가의 분모·정렬·빈 출력 보고 | 공개 논문/도구와 기존 보고 방식 대조 | 다음 실제 변경을 평가할 때 보완할 가치. 실행 경로 변경 불필요 |
| Smart Turn·confidence 즉시 확정·새 VAD | 공개 구현의 조건 확인 | 방송 자막·짧은 구간의 품질/지연 근거 부족으로 후순위 |

큰 모델 변경이나 기존 실패 설정을 다시 시도하는 작업으로 확대하지 않는다. 새로운 구현을 한다면 변수별 판정과 원문/번역 의미·호출수·지연 비악화를 먼저 명시한다. **이번에 확인한 개선 가능성과 실제 효과를 구분하며, 추가 모델 시험은 수행하지 않았다.** 앱 코드·사용 설정·모델 파일·최근 번역 기록은 유지하고, E드라이브 복사·배포·화면 측정은 하지 않았다.

## 15. 심층 조사 후보 D1·D2·D3 실제 시험 — 2026-10-05

사용자의 “테스트 해봐” 요청에 따라 §14의 세 후보를 격리 시험했다. **세 후보 모두 미채택이다.** 현재 Qwen3-ASR-1.7B + HY-MT2 기본 경로와 Whisper 설정을 유지한다. 기존 모델을 참조했고 추가 다운로드나 가중치 복사는 없었다. GPU 시험은 RTX 4080에서 순차 실행하여 후보끼리 자원을 경쟁하지 않게 했다.

이번 결과는 고정 입력에 대한 번역 또는 ASR 함수 완료 시간이다. 음성 수집·분절·큐·두 줄 읽기 대기·화면 표시까지의 전체 지연과 구분한다. 화면 측정, Qwen StaticCache/decoder compile, 힌트 추가, 새 ASR 모델 비교는 실행하지 않았다.

### 15.1 D1 — HY 샘플링 순서: 의미 개선·속도 이득 미확인

- 현재 앱의 `Runtime.translate`와 HY-MT2-7B-Q6_K/llama b11378을 사용했다. 원문·문맥·프롬프트·EOS 보정127960·반복 패널티 범위64·권장 수치를 유지했다.
- 후보는 기본 sampler 전체 배열에서 temperature만 top_k 직전으로 이동했다. 서버 `/props`와 `/slots`에서 실제 적용 순서를 확인했다. sampler 외 공통 요청 본문 해시는 모든 대응쌍에서 같았다.
- 기존 영상 ASR 원문35개, 기존 통제26개, 새 부정·수량·인명·자기정정 통제12개 = **73개 × 양쪽3회 = 438번역/219쌍**. ABBAAB 순서와 대응 seed를 사용했다. 현재 문맥 제한이나 반복 패널티 범위를 함께 바꾸지 않았다.
- 캐시/이전 생성 길이의 영향을 분리하기 위해 양쪽 모두 `cache_prompt=false`로 통제했고 실제 cache_n=0을 확인했다. 실제 앱의 캐시 사용 경로와 같다고 주장하지 않는다.
- 준비 확인 run-1은 canonical sampler 이름 `typ_p`를 확인하는 단계에서 종료했다(측정0회). 표기를 맞추고 실제 배열 검증을 통과한 **run-2만 집계**했다.

| 번역 함수 시간 | 기존 | 순서 변경 |
|---|---:|---:|
| 전체219회 p50 | 180.13ms | 181.81ms |
| 전체219회 p95 | 487.00ms | 481.59ms |
| 영상105회 p50 | 192.89ms | 196.07ms |
| 영상105회 p95 | 442.98ms | 446.43ms |
| 전체 출력 토큰 평균 | 17.3516 | 17.3516 |
| 측정 오류 / 재시도 | 0 / 0 | 0 / 0 |

대응 요청의 후보-기존 차이는 전체 p50 **+1.88ms**, 영상 p50 **+2.35ms**였다. p95의 일부 하락만 골라 가속으로 해석하지 않는다. 표본 구성이 다르므로 이 전체180ms를 과거 영상 MT 약250ms와 직접 비교하지도 않는다.

219쌍 중 **204쌍은 문자열까지 동일**했다. 설정 이름을 X/Y로 가린 텍스트 검수에서 나머지15쌍은 의미상 동률14쌍, 표준 인명 표기가 없어 우열 불확정1쌍이었다. 확인된 방향성 있는 의미 개선은 없었다. 이는 assistant 기반 정성 검수이며 원어민 복수 평가나 공인 한국어 정답 점수가 아니다.

두 설정이 같은 오역을 만드는 사례도 남았다. 예를 들어 영상 입력 `关我的事儿。`에 두 방식 모두 없는 부정을 넣었고, 미완성 입력을 완성하면서 의미가 바뀔 위험이 있었다. 통제 문장의 `号`를 `일`로 옮기거나 목격자의 주어를 생략하는 현상도 공통이었다. 실제 ASR 원문을 번역 평가 기준으로 삼았으며 검수 SRT로 원문을 몰래 보정하지 않았다. 이 관측만으로 실제 발화의 의도를 확정하지 않는다.

**판정:** 이번 표본에서 의미·속도 이득이 없어 미채택. 1차 근거를 통과하지 못했으므로 캐시 사용 Qwen+HY 전체 replay나 제품 변경으로 확대하지 않았다. 샘플링 순서가 수학적으로 다르다는 §14의 확인은 유지하지만 그것이 앱 개선을 뜻하지 않는다.

근거: [D1 결과 요약](../../work/hy-sampler-test-20261005/D1_결과.txt), [판정](../../work/hy-sampler-test-20261005/decision.json), [73개 입력·조건](../../work/hy-sampler-test-20261005/run-2/manifest.json), [438번역 원시 기록](../../work/hy-sampler-test-20261005/run-2/records.jsonl), [통계](../../work/hy-sampler-test-20261005/metrics.json), [영상 블라인드 검수](../../work/hy-sampler-test-20261005/review-video.json), [통제 검수](../../work/hy-sampler-test-20261005/review-controls.json), [실행·보존 결과](../../work/hy-sampler-test-20261005/run-2/results.json).

### 15.2 D2 — Qwen 정상 검사의 문자열 생성: 비용은 줄지만 체감 개선 근거 없음

설치된 Transformers 5.18의 `torch_compilable_check`는 오류 문구를 callable로 받을 수 있다. `Qwen3ASRModel.get_placeholder_mask`의 오류 설명 f-string 한 곳만 `lambda: f-string`으로 감싼 메모리 내 후보를 만들었다. AST 역변환으로 다른 구문이 바뀌지 않았는지 확인했다. 입력 일치 검사와 예외 종류·오류 문구를 그대로 유지했으며 설치 파일을 수정하거나 검사를 꺼버리지 않았다.

- CPU 정상/불일치 입력 **28사례**에서 결과 마스크·예외·문구가 같았고 CUDA 미초기화를 확인했다. 별도 CUDA 정상/불일치 검사도 동일했다.
- 100회 묶음 × 설정별3회 helper 측정에서 1회당 중앙값 **0.183217→0.162595ms**, 약 **0.0206ms** 감소했다.
- 별도10회 profiler에서는 `.item()` **10→0**, stream synchronize **20→10**으로 줄었다. profiler 시간과 실제 benchmark 시간을 섞지 않았다.
- 실제 저장 PCM의 **0.9초/4초 두 구간 × 설정별3회 = 12번 ASR/6대응쌍**을 실행했다. 중국어 고정·힌트 없음, 양쪽 길이별 사전 준비 후 교대로 비교했고 원문·언어는 모두 동일했다.
- 실제 ASR 6회 합계는 기존 **2.594508초**, 후보 **2.597072초**였다. 대응 차이는 약 -10.05~+5.76ms 범위로 흔들려 미세 helper 절감을 실제 인식 가속으로 확인하지 못했다.

**판정:** 작은 비용의 원인은 확인했으나 앱에서 체감할 가속 근거가 없어 미채택. 두 음성 구간의 소규모 ASR 시험이며 전체 자막 지연/품질을 일반화하지 않는다. 라이브러리 영구 monkeypatch나 새 환경 변수는 추가하지 않았다. 이후 공식 upstream이 바뀌면 새 소스와 다시 대조할 수 있지만 같은 조건의 반복 미세 최적화를 기본 과제로 삼지 않는다.

근거: [D2 결과 요약](../../work/qwen-scalar-test-20261005/결과_요약.txt), [판정](../../work/qwen-scalar-test-20261005/decision.json), [CPU28사례](../../work/qwen-scalar-test-20261005/cpu-check.json), [실제 ASR 비교](../../work/qwen-scalar-test-20261005/isolated-1/report.json), [helper 측정·프로파일](../../work/qwen-scalar-test-20261005/isolated-1/helper.json), [원본·메서드 복원 검사](../../work/qwen-scalar-test-20261005/isolated-1/preserved.json).

### 15.3 D3 — Whisper 시각 토큰 생략: 꼬리 재인식과 불필요한 출력 증가

현재 `Runtime.transcribe_stream`을 그대로 호출하고 메모리 내 요청에 `without_timestamps` 하나만 False/True로 바꿨다. **`word_timestamps=True`는 양쪽 유지**했다. 기존 whisper-large-v3-turbo/CT2 **int8_float16**, 중국어 고정, 힌트 없음, beam5, temperature0, 현재 VAD/품질 필터도 유지했다.

기존 영상 PCM의 0·20·30·60·68·80초에서 각각4초를 잘라 ABBAAB로 **각 설정3회, 총36회** 시험했다. 양쪽의 첫 노출 블록0/1은 반복 통계와 분리하고 남은 설정별12호출을 집계했다.

| 반복 ASR 함수 시간/동작 | 기존 | 시각 토큰 생략 |
|---|---:|---:|
| 12호출 p50 | 149.38ms | 285.80ms |
| 12호출 평균 | 151.48ms | 322.22ms |
| 12호출 최대 | 197.60ms | 464.42ms |
| 4초 입력당 encode/generate/align 각각 | 1회 | 3~5회 |

모든6입력에서 후보에 불필요한 뒤 문구가 붙었다. 30~34초에는 `字幕志愿者 杨茜茜`, 68~72초에는 `优优独播剧场——YoYo Television Series Exclusive`가 추가됐다. 기존 검수 SRT의 해당 구간과 대조해 이 문구들이 없음을 확인했다. 기준 인식에도 틀린 단어/인명 누락이 있으며 그것을 이번 후보만의 결함으로 세지 않았다.

단어 정렬 전 `seek_after_split`만 보고 원인을 단정하지 않았다. 30초 입력의 다음 실제 split 진입 위치 `seek_before`가 **362→398프레임**으로 진행했고, 100fps 기준 **0.38초·0.02초의 꼬리를 다시 추론**했다. 현재 faster-whisper가 마지막 단어 시각으로 seek를 되돌리는 §14의 우려가 이 입력에서 실제 추가 encode/generate/align과 함께 재현됐다.

**판정:** 속도·내용 모두 악화되어 미채택. 현재 Whisper의 기본 `without_timestamps=False`와 `word_timestamps=True`를 유지한다. 짧은 고정 crop 시험으로 전체 SRT CER나 한국어 완료 지연을 계산하지 않았고, LocalAgreement/전체 영상 replay로 확대하지 않았다. 이 옵션이 모든 Whisper 사용 환경에서 나쁘다는 보편적 결론은 아니다.

근거: [D3 결과 보고서](../../work/whisper-timestamp-test-20261005/D3_결과.txt), [판정](../../work/whisper-timestamp-test-20261005/decision.json), [구간별 비교](../../work/whisper-timestamp-test-20261005/comparison.txt), [36회 원시 결과·보존 검사](../../work/whisper-timestamp-test-20261005/gpu-comparison.json), [통계·원문·단어 시각](../../work/whisper-timestamp-test-20261005/analysis.json), [실험 설계와 한계](../../work/whisper-timestamp-test-20261005/preparation.txt).

### 15.4 다음 변경에서 유지할 결정

| 후보 | 결정 | 같은 시험을 다시 하지 않을 이유 / 재개 조건 |
|---|---|---|
| D1 HY sampler 순서 | 미채택 | 의미 개선·속도 이득 없음. 순서 변경으로 반복 오류를 해결하는 새 표본 또는 모델/런타임 변경 때만 재검토 |
| D2 Qwen 오류 문자열 lazy 생성 | 미채택 | helper 약0.02ms는 실제 ASR 이득으로 확인되지 않음. 작은 비용을 초 단위 지연 개선책으로 재제안하지 않음 |
| D3 Whisper 시각 토큰 생략 | 미채택 | 현재 짧은 입력에서 꼬리 재인식·불필요한 출력·지연 증가. 관련 라이브러리 동작 변경 등 실패 원인을 해결할 새 근거가 필요 |

실험용 변경은 프로세스 메모리와 `work/`에만 있었으며 앱 실행 코드·설정·모델을 변경하지 않았다. 검사한 보호 파일의 해시, 가중치 크기/수정시각, 메서드 복원을 각 결과에 남겼다. 시험 프로세스는 종료했다. 이 절과 상단 판단표는 새 결과를 기록하기 위해 갱신했으며 E드라이브·배포본에는 복사하지 않았다.

## 16. 현재 로컬 Qwen과 Gemini Live 실제 영상 비교 — 2026-10-05

사용자의 “테스트 영상으로 로컬이랑 제미나이라이브랑 비교 해봐” 요청에 따라 기존 `ndhgOQNXx9M` 영상 음성을 **각3회, 총6회** 실시간 속도로 공급했다. **이번 영상에서는 현재 로컬 Qwen 경로가 원문 정확도와 공통 대사 전달 지연에서 유리했다.** Gemini가 긴 문장 관계를 더 잘 유지한 구간도 있으므로 모든 대사에서 로컬이 더 좋다는 뜻은 아니다. 앱 실행 코드·설정·모델은 변경하지 않았다.

### 16.1 비교 조건과 측정 범위

- 로컬은 **Qwen3-ASR-1.7B / 현재 빠른 경로(legacy)**, 문장 경계 재확인 꺼짐, 중국어 지정, 힌트 없음이다. Gemini도 중국어를 지정했다.
- Gemini는 앱의 저장된 키로 실제 API에 접속했다. 요청 모델 별칭은 **`models/gemini-3.5-transcribe-live`**이며 setup 수락을 확인했다. 응답에서 서버의 구체적인 모델 버전은 확인되지 않아 별칭 이상으로 버전을 확정하지 않는다.
- 양쪽 번역은 같은 **HY-MT2-7B-Q6_K / 현재 프롬프트·문맥·용어집·샘플러**를 사용했다. §15 D1 후보는 적용하지 않았다. Gemini 자체 한국어 번역과의 비교가 아니다. 기존 실제 앱처럼 번역 seed를 고정하지 않아 한국어 표현 일부는 반복마다 달랐다.
- RTX 4080에서 로컬1→Gemini1→Gemini2→로컬2→로컬3→Gemini3 순서로 새 프로세스에서 실행했다. 모델 적재·로컬 ASR 준비 추론은 음성 공급 전에 완료하고 준비 시간과 자막 시간을 분리했다. Gemini 음성 추론과 HY의 첫 실전 번역은 음성 공급 이후 실행됐다.
- 같은 **16kHz mono PCM16LE 99.822625초**를 100ms 묶음으로 실시간 공급하고 양쪽 모두 끝에 2초 무음을 넣었다. 강제 `audioStreamEnd`나 강제 확정은 넣지 않았다. Gemini 전송량은 매회 3,258,324바이트로 같았다.
- 프로덕션 `Runtime`·`StreamSession`을 사용하고, 테스트용 이벤트 기록과 llama 로그 경로만 격리했다. 브라우저 재생·WASAPI 캡처·WPF 렌더링·오버레이 읽기 대기는 거치지 않았다. **속도는 엔진 자막 이벤트 로그이며 화면 표시 지연이 아니다.**
- Gemini는 공급자 대기·빈 가설·빈 큐·전송 바이트를 함께 확인한 뒤 종료했다. 모든 실행에서 남은 번역 작업·미확정 가설이 없었고 보호한 제품 파일이 보존됐다. 사용자 최근 번역 기록에 테스트 자막을 넣지 않았다.
- 기준은 이미 이미지 대조한 중국어 SRT다. 이번에는 음성을 새로 듣고 별도 정답을 만들지 않았다. 기존 정책대로 첫33ms cue1을 제외한 **38cue / 정규화277자**를 주 점수로 사용했다. 전체39cue 결과도 분석 파일에 보존했다. SRT·한국어 참조는 모델에 주입하지 않았다.
- 원본 PCM SHA256: `7dca4122126bcf30709b24cd8690c1d62dbdf95e03976722822f009b2f072f55`; SRT SHA256: `2e23a08b1e96461604f455c688881a2f4d8b958dc5c54b83201999ed80317109`.

### 16.2 원문 품질·전달 지연 결과

| 항목 | 로컬 Qwen + HY | Gemini Live + HY |
|---|---:|---:|
| 원문 문자 오류율 CER — 낮을수록 좋음, 3회 각각 동일 | **15.88%** | **24.19%** |
| 편집 오류 / 참조 문자 | 44 / 277 | 67 / 277 |
| 치환 / 삭제 / 삽입 | 16 / 6 / 22 | 37 / 20 / 10 |
| 원문 정확 일치 cue / 38 — 매회 동일 | 17 / 38 | 16 / 38 |
| 부분·다른 문자 정렬 cue / 38 | 20 / 38 | 20 / 38 |
| 대응 문자가 없는 cue / 38 | 1 / 38 | 2 / 38 |
| 첫 한국어 자막 이벤트 — 3회 중앙값 | **5.19초** | **8.06초** |
| 같은12개 대사의 확정 자막 전달 지연 — 36관측 중앙값 | **1.20초** | **1.52초** |
| 같은12개 대사 지연 p95 | 3.47초 | 3.87초 |
| 최종 원문 / 최종 한국어 자막 수 — 매회 | 35 / 35 | 29 / 29 |
| 번역 호출 수 — 1·2·3회 | 35 / 35 / 35 | 37 / 35 / 37 |
| 잠정 한국어 이벤트 수 — 매회 | 0 | 7 |
| 번역 실패 / 미번역 확정 원문 / 모델 호출 오류 — 모든 실행 | 0 / 0 / 0 | 0 / 0 / 0 |

CER는 최종 원문과 최종 caption의 `source_text`에서 각각 계산했으며 이번에는 두 결과가 같았다. CER를 한국어 번역 정답률이나 `100-CER` 형태의 인식 정확도로 바꿔 쓰지 않는다. 문장 수 차이도 분할 방식 차이를 포함하므로 35대29 자체를 누락 수로 해석하지 않는다. 동일 방식의 확정 중국어 문장열은 3회 완전히 동일했고, 한국어의 일부 어휘·말투만 달랐다.

첫 한국어는 로컬 **5.360 / 5.171 / 5.187초**, Gemini **8.062 / 8.047 / 8.094초**였다. 세 번 모두 최초 자막이 확정 이벤트였다. 다만 로컬은 소개를 중간에 끊어 먼저 번역하고 Gemini는 더 긴 소개를 모아 번역했다. 따라서 약2.9초의 첫 자막 차이를 이후 모든 대사의 차이로 확대하지 않는다.

공통 지연은 모든6회에서 같은 원문 구절이 한 자막에 명확히 대응하는 **cue 5·6·14·16·17·20·22·25·26·31·32·36, 총12/38개**만 사용했다. `확정 한국어 이벤트 시각 - SRT cue 끝 시각`으로 계산했다. 잘못 인식되거나 여러 자막에 나뉜 나머지 구간의 시각을 억지로 채우지 않았다. SRT 끝은 실제 음절 종료 정답과 다를 수 있으며 원문이 맞는다고 한국어 의미까지 맞았다는 뜻도 아니다. 공통 집합의 cue14도 §16.3의 번역 오류를 포함한다. 일부 cue는 Gemini가 더 빨랐고, 이 중앙값은 **선별된 같은12대사의 전달 지연**이다.

계측에 사용한 기존 `time.monotonic`은 이 Python 환경에서 GetTickCount64/15.625ms 분해능이다. 저장값이 밀리초처럼 보여도 수ms 차이의 우열을 논하지 않는다. 최종 표시 수치는 초 단위 소수 둘째 자리로 제시했다. 3회씩은 한 영상의 반복성 확인이며 여러 콘텐츠/언어의 독립 표본이 아니다.

모델 준비는 로컬 **20.094 / 12.796 / 11.641초**, Gemini **4.016 / 3.922 / 4.016초**였다. 로컬 ASR 적재·준비와 같은 HY 준비를 포함하는 별도 구간이며 자막 전달 지연 표에 더하지 않았다. 클라우드 수신 간격을 Google 내부 추론 시간으로 해석하지 않는다.

SRT는 93.333초에 끝나고 뒤 LiTV 안내 음성은 기준에 없다. 주 CER에는 해당 꼬리 삽입도 그대로 남겼다(로컬 마지막 참조 뒤12자, Gemini8자). 기준에 없는 브랜드 음성을 전부 환각이라고 단정하거나 유리한 점수를 만들기 위해 임의로 제거하지 않았다. 첫33ms 제외와 꼬리 삽입의 영향을 포함한 상세는 각 `analysis.json`에 있다.

### 16.3 실제 의미 대조 — 양쪽 장단점

| 검수 자막의 의미 | 로컬 결과 | Gemini 결과 | 판단 |
|---|---|---|---|
| 웨딩드레스에 어울리려면 갈비뼈 두 개를 빼야 한다 | 수량·갈비뼈·드레스 보존 | `愁了半夜才配得上那份少` → 밤새 고민했다는 내용 | Gemini 공급자 확정 원문부터 다름. 로컬 우위, 3회 재현 |
| 돈 내놔 (`把钱给我`) | 원문·한국어 모두 존재 | 최종 원문·한국어에서 없음 | Gemini 원문 단계 누락, 3회 재현 |
| 필사적으로 올라가려는 꼴이 역겹다 | 핵심 경멸 유지 | 뒤 문장이 `身上热热的` → 몸이 뜨겁다 | Gemini 인식 내용 변화, 3회 재현 |
| 두 사람의 세기의 결혼식 소개 | 인명 앞뒤 분리, 世纪→实际 → 실제 결혼식 | 소개 관계와 世纪 유지 | 이 구간 Gemini 우위. 양쪽 인명 자체는 틀림 |
| 예전처럼 당하는 구샤오탕인 줄 알아? | 경계에서 欺负的 반복, 두 질문처럼 번역 | 자기 지칭을 한 문장으로 유지 | Gemini 문장 연결이 유리 |
| 처음부터 내가 그의 표적이었나? | 질문이 분리돼 ‘그게 그의 목표’로 변함 | 전체 질문 관계 유지 | Gemini 문장 연결이 유리 |
| 이 모든 걸 지금 부숴버리자 | 앞 조각 `就让这一切` → 그냥 이대로 둬 | 같은 앞 조각을 그냥 이대로 두라고 번역 | 공통 분절+번역 오류. CER만으로 못 잡는 문제 |
| 반복/시간 루프에 대해 어떻게 아는가 | 循环→雪花 → 눈송이 | 循环→是我的 → 내 일 | 둘 다 인식 원문 단계 불일치 |

별도로 로컬의 부정확한 원문 `关我的事儿`에는 부정이 없는데 HY가 “내 알 바 아니야”로 옮겼다. 원문 자체의 오인식과 번역의 부정 추가를 구분한다. 모든 오류를 ASR 탓으로 돌리지 않는다. 인명의 糖/棠/唐 동음자 차이와 한국어 발음/한자음 표기 정책도 실제 의미 반전과 구분했다. 좋은 결말의 자격이 없다는 부정·죽어서도 놓지 않겠다는 위협·다시 돌아갈 수 없다는 부정·세 번째 반복의 횟수는 양쪽 모두 핵심을 보존했다. [10사례 상세와 반복성](../../work/local-gemini-compare-20261005/의미비교.txt).

### 16.4 Gemini 공급자 원문 보존과 별도 잠정 표시 문제

공급자 로그까지 대조한 결과, 각 실행의 **25개 `inputTranscription` → 앱 확정 원문29개**는 문장 분할 이후에도 전체/턴별 내용이 보존됐다. 공급자 확정 원문 시퀀스도 3회 동일했다. `generationComplete`는 매회25개였고 `finished`·`turnComplete`는 관측되지 않았다. 이번 차이를 `finished=false`를 무시한 조기 확정으로 설명할 근거는 없다. “갈비뼈” 등이 바뀐 내용은 공급자의 원문부터 존재했고 앱 후처리에서 바뀐 것이 아니다.

다만 **최종 CER와 별개인 잠정 처리 문제**가 실제 로그에서 확인됐다.

1. **이미 확정한 대사가 다음 잠정 전사에 다시 포함됨:** Gemini2·3회에서 약74.3초에 확정한 두 문장이 다음 `interimInputTranscription`에 새 대사와 함께 재포함됐다. 앱은 이를 새 턴/새 ID의 잠정으로 번역했다가 다음 final에서 교정·철회했다. 잠정 반복이 이벤트 상태에 남은 시간은 약0.11~0.47초다. 실제 화면 체류 시간은 측정하지 않았다. “interim은 항상 마지막 final 이후의 새 턴 범위”라는 가정은 이 표본과 맞지 않는다.
2. **잠정 문장 둘이 확정 한 문장으로 합쳐질 때 내용 겹침:** Gemini1회에서 합친 새 번역과 기존 단독 질문이 이벤트 상태에 약0.53초 함께 남았다. 최종 결과에는 중복이 남지 않았다.
3. **공백·전각/반각 구두점만 바뀌어 재번역:** 좁은 NFKC+공백 정규화에서 같아지는 연속 갱신은 1·2·3회에 3·1·1건이었다. 동등한 입력의 갱신 비용을 줄일 후보지만 아직 패치하거나 효과를 검증하지 않았다.

이 문제들은 기존 Qwen 미세 최적화 실패와 다른, **이번 실제 Gemini 로그로 새로 확보한 후속 검증 대상**이다. 실제 반복 발화를 무조건 제거하는 전역 문자열 필터를 넣어 해결하지 않는다. 수정한다면 이 원시 이벤트를 CPU 재생해 이전 확정문 재포함·진짜 반복 발화·final 병합/철회·두 줄 교체를 함께 검증해야 한다. 현재 비교 중에는 동작을 바꾸지 않았다. [3회 공급자/앱 이벤트 상세](../../work/local-gemini-compare-20261005/provider-observation.txt).

### 16.5 유지할 판단과 재실행 조건

이번 영상과 현재 설정에서는 **로컬 Qwen + HY를 유지할 근거가 충분하며, Gemini가 무조건 더 정확하거나 빠르다는 이전 인상을 지지하지 않는다.** 다만 로컬의 문장 분절로 인한 번역 오류가 남고 Gemini가 더 잘 이어 번역한 사례도 있으므로 전체 품질 문제가 해결됐다고 쓰지 않는다. 다른 음질·영상·언어나 서버 버전에서의 결과까지 일반화하지 않는다.

같은6회를 이유 없이 반복할 필요는 없다. 새 영상/언어, 실제 오류 표본, 모델·서버·분절 로직 변경, §16.4의 좁은 수정 검증이 있을 때 목적을 명시하고 다시 비교한다. Gemini 연결/잠정 표시 수정을 채택하려면 최종 원문 보존뿐 아니라 잠정 반복·번역 호출·대응 대사 지연도 함께 확인한다.

API 키는 메모리에서 사용하고 결과 파일에는 키·요청 URI·인증 헤더를 저장하지 않았다. 실제 Google 음성 전송은 3회 합계 **305.467875초(각 영상+2초 무음)**였으며 청구액/청구 기준 시간은 확인하지 않았다. 실행·분석 기록만 `work/`에 남기고 모델 복사, 제품 코드/설정 변경, 최근 기록 변경, E드라이브/배포본 복사는 하지 않았다.

근거: [종합 비교 결과](../../work/local-gemini-compare-20261005/비교결과.txt), [6회 집계·조건·지연](../../work/local-gemini-compare-20261005/comparison.json), [평가 설계 검토](../../work/local-gemini-compare-20261005/평가설계_검토.txt), [실행 하네스](../../work/local-gemini-compare-20261005/run.py), [분석기](../../work/local-gemini-compare-20261005/analyze.py), [의미 비교](../../work/local-gemini-compare-20261005/의미비교.txt), [공급자 이벤트 검토](../../work/local-gemini-compare-20261005/provider-observation.txt), [마지막 보존·완료 검사](../../work/local-gemini-compare-20261005/final-verification.json). 각 `local-1/2/3`, `gemini-1/2/3` 폴더의 `run-report.json`·원시 이벤트·`analysis.json`·cue별 CSV에 세부 결과가 있다.

## 17. 4개 입력·3 ASR·2 번역 모델의 72회 비교 — 2026-10-05 완료

사용자 요청으로 **4개 입력 × Qwen/Whisper/Gemini × HY/MiLMMT × 각3회 = 새 72회**를 실행했다. 모두 비교 가능한 상태로 완료했으며 실행 실패·제외는 0회다. Gemini 24회도 API 사용량 제한 없이 완료했다. §16의 이전 6회 결과는 이번 반복에 재사용하거나 합산하지 않았다. 전체 표와 원문·한국어 사례는 [전사모델비교.md](전사모델비교.md)에 기록했다.

### 17.1 고정 조건과 평가 범위

- 기존 `ndhgOQNXx9M` 영상은 전체 99.822625초, 추가 `10분이상샘플`의 1of3/2of3/3of3 파일은 각각 **0~180초**만 사용했다. 추가 파일들은 같은 중국어 드라마 에피소드의 세 부분이며, 독립된 세 장르가 아니다.
- 현재 C# `StreamingPcm16Converter`로 준비한 mono 16kHz PCM16LE를 100ms씩 실시간 공급했다. 실행마다 모델·세션을 새 프로세스에서 준비하고 순서를 바꿔 반복했다. ASR 준비 추론은 입력 전에 수행했으며 실제 번역 첫 호출은 입력 시작 후다. 끝에는 2초 무음을 보냈고 강제 final로 결과를 만들어내지 않았다.
- Qwen3-ASR-1.7B BF16의 현재 빠른 경로, Whisper large-v3-turbo `int8_float16`의 현재 legacy 경로, 요청 ID `models/gemini-3.5-transcribe-live`를 사용했다. Gemini 내부 서버 모델 버전은 응답에서 확인하지 못했다.
- 번역 파일은 `HY-MT2-7B-Q6_K.gguf`, `MiLMMT-46-12B-v1.0.i1-Q4_K_M.gguf`다. 각 모델의 현재 번역 프로필을 유지했다. HY는 앞선 성공 확정 원문 최대3개·용어집·샘플링을 쓰고 MiLMMT는 이전 문맥/용어집 없이 greedy completion을 쓴다. **단일 가중치만의 공정한 독립 순위가 아니라 현재 앱 조합 비교**다.
- 입력 중국어 고정, ASR 힌트 빈 값, Qwen 문장 경계 재확인 꺼짐이다. 기존 HY 번역 용어집 5항목은 유지했으므로 인명 표기 우위를 모델 자체의 실력으로 돌리지 않는다. 참조 SRT를 ASR/번역 입력으로 주입하지 않았다.
- 기존 검수 SRT는 첫 33ms cue를 이전 규칙대로 제외했다. 추가 SRT는 제공된 참고 자막이며 전체 문구·동기를 독립 검수한 정답은 아니다. SRT 밖의 실제 안내 음성·감탄·반복을 자동으로 환각이라 판정하지 않았다.
- 지연은 엔진 이벤트와 SRT cue 종료의 차이다. **화면 표시 시각·독자 체감·실제 음향 발화 종료를 측정한 값이 아니다.** 앱 화면 계측을 추가하지 않았다.

### 17.2 원문 일치도와 대응 대사 지연

아래는 정규화한 확정 원문과 SRT의 **참고 CER(%)**다. 낮을수록 참고 자막과 가깝다. 이번 실행에서는 각 ASR의 3회 반복 및 두 번역 조합 사이에서 같은 값이 나왔다. 참조 오류·비참조 음성·분절의 영향도 포함하므로 그대로 실제 음성 인식 오류율이라고 단정하지 않는다.

| 입력 | Qwen3-ASR-1.7B | Whisper large-v3-turbo | Gemini Live |
|---|---:|---:|---:|
| 기존 영상 | 15.88 | 19.49 | 24.19 |
| 추가1 | 7.06 | 13.51 | 12.10 |
| 추가2 | 9.32 | 11.86 | 12.50 |
| 추가3 | 10.07 | 15.97 | 19.10 |

네 입력의 평가 범위는 **총222개 참고 대사·정규화 원문1533자**다. 문자 편집 오류를 합산한 전체 참고 CER은 Qwen **152/1533=9.92%**, Whisper **223/1533=14.55%**, Gemini **241/1533=15.72%**다. 영상별 백분율의 단순 평균이 아니다. 전체 정렬에서 문자 완전 일치/일부 차이/대응 문자 없음은 각각 Qwen **138/81/3**, Whisper **118/98/6**, Gemini **114/101/7**이며 각 합계는222다. 대응 문자 없음은 실제 음성 누락 확정이나 번역 실패와 같지 않다.

사용자 지적에 따라 보고서 앞부분은 **행=테스트영상·샘플1·샘플2·샘플3·합계, 열=ASR 모델**로 정리하고 전체222개 분모의 원문 평가를 우선 표시했다. 모든 6조합·3회에 원문이 정확히 대응한 공통 cue의 지연은 **9/38, 21/68, 14/67, 15/49**, 합계 **59/222개**에 한정된 보조 지표다. 따라서 빠졌거나 잘못 인식한 대사까지 포함한 전체 지연 점수나 전체 속도 순위로 읽으면 안 된다. 개별 조합의 신뢰 가능한 시각 연결도 Qwen130/222, Whisper HY89/222·MiL88/222, Gemini112/222뿐이다. 나머지를 0초나 임의 지연으로 채우지 않고 판정 불확실로 표시했다. 후속 요청으로 평가한 전체222개 한국어 내용 적합도는 §17.5에 기록했으며 아래35사례 정성 검토와 구분한다.

아래는 **HY 조합의 공통 cue 확정 지연 중앙값(초)**이다. MiLMMT 및 각3회/p95/첫 이벤트 값은 전체 비교표를 참고한다.

| 입력 | Qwen+HY | Whisper+HY | Gemini+HY |
|---|---:|---:|---:|
| 기존 영상 | 1.21 | 3.76 | 1.88 |
| 추가1 | 1.17 | 2.13 | 1.72 |
| 추가2 | 2.68 | 1.67 | 4.16 |
| 추가3 | 1.06 | 2.68 | 1.52 |

잠정이 있는 경로를 확정 시각만으로 불리하게 해석하지 않도록 **최종 원문·한국어와 동일한 첫 이벤트**도 따로 분석했다. 동일 세션/ID에서 공백만 무시하며, 문구 변경·실패·철회로 끊긴 이전 수명을 재사용하지 않는다. A→B→A라면 돌아온 A부터 센다. 최초로 뜻이 비슷한 자막이나 실제 화면 표시 시각을 뜻하지 않는다. 이 보조 지연에서도 기존/추가1/추가3은 Qwen, 추가2는 Whisper의 중앙값이 가장 짧았다.

추가2 Gemini+HY의 확정 p95 **14.90초**를 ‘한국어가 처음 나오기까지 15초’라고 설명하면 틀린다. 예를 들어 cue56의 1회 실행은 SRT 종료155.383초, 최종과 동일한 잠정159.531초, 확정170.578초다. 같은 문구 첫 이벤트 지연은 **4.148초**, 확정은 **15.195초**였다. 다른 실행에서 구두점이나 번역이 달라지는 경우를 동일 문구로 느슨하게 합치지 않았다. 이 입력의 252개 cue-실행 대응을 원시 이벤트로 별도 검사했으며 연결·시각 산술 오류는 없었다.

### 17.3 번역 의미·실패를 별도로 확인한 결과

기존5개·추가 각10개, 총 **35개 고정 사례 × 18회 = 630개 사례-실행 대응**을 원문과 한국어로 검토했다. 같은 사례/반복을 포함하므로 630개의 독립 표본이 아니며, 한국어 정답 전체나 원어민 복수 평가에 의한 정확도 점수도 아니다. 추가30개는 이번 실행 결과를 보기 전에 선정했고 기존5개는 이전 시험에서 알려진 사례다.

- 현재 조건에서는 **Qwen+HY 유지가 타당하다.** Qwen의 참고 CER가 4개 입력에서 모두 가장 낮았다. 다만 Qwen도 짧은 부정·연속 발화 경계에서 의미를 놓치고, Gemini가 긴 질문의 관계를 더 온전하게 유지한 사례가 있었다.
- MiLMMT가 HY의 일괄적인 상위 대체 모델이라는 근거는 없었다. `你先生着气`의 `先生`을 남편으로 오해하지 않은 MiLMMT 사례가 있는 반면, 혼인 관계·`多余`·`小气`를 잘못 옮긴 경우도 있었다. HY도 이전 문맥까지 다시 번역하거나 같은 원문에 실행별 표현·의미가 달라지는 사례가 남았다.
- 추가1 Whisper+MiLMMT는 같은 소개 대사 **2개가 3회 모두 최종 번역 실패**했다. 합계6개 최종 원문에 성공한 최종 한국어가 없었으며 이전 잠정 한국어가 있었다고 최종 성공으로 세지 않았다.
- 추가2 Gemini+MiLMMT 2회차의 잠정 번역 실패1건은 이후 원문 교정과 함께 성공했고 final도 정상 완료됐다. 따라서 실패 caption 이벤트는 총7건, 최종 미번역 원문은6개다. 함수 수준 번역 오류10건·잠정 실패·최종 실패·실행 전체 실패는 서로 다른 분모다.
- 실패 코드 `translation_language`만 남은 호출은 거부된 모델 원문을 저장하지 않았다. 영어 출력인지 거절인지 추정하지 않으며 Google 할당량 오류로 분류하지 않는다. `local_streaming_deadline` 경고36건도 숨기지 않고 전체 표에 기록했다. 모든 실행의 입력·후처리 큐 종료를 확인했다.
- 참고 SRT의 실제 반복 `不`·`疼` 등을 전역 중복 제거로 지우는 수정은 근거가 없다. 실제 누락, 강제 경계의 잘못된 합침, 잠정 재포함, 번역 문맥 재출력은 별도로 검증해야 한다.

### 17.4 보존·판정·다음 재실행 조건

**이번 요청은 비교 시험이며 제품 수정은 하지 않았다.** 엔진/설정/기존 로그·기록의 보존, 입력 영상·SRT·PCM 해시, 모델 파일 크기·수정 시각, Google 전송량, 분석 근거/문서 링크, 큐 종료와 시험 소유 프로세스 종료를 최종 검사했다. 결과는 `verified_complete`, 검사 **1,964개 통과**다. 모델 가중치 복제, E드라이브/배포본 복사는 없었다.

Google 음성 전송은 24회 합계 **3,886.93575초(약64.78분, 종료 무음 포함)**였다. 이는 전송한 오디오 분량이며 실제 청구액/청구 기준 시간은 확인하지 않았다. API 키·요청 URI·인증 헤더는 결과에 저장하지 않았다.

동일72회를 이유 없이 반복하지 않는다. 새로운 입력/언어, 모델·런타임 변경, 실패 표본을 겨냥한 패치가 있을 때 바뀐 조건을 명시한다. 다음 후보는 최종 번역 실패6개, HY의 이전 문맥 재출력, 짧은 부정·문장 경계 누락 같은 **확인된 개별 실패**를 먼저 재생해 검증하는 것이다. 변경을 채택하려면 원문·의미 보존과 확정/잠정 지연을 함께 확인한다. 이번 결과만으로 새 필터·잠정 표시·모델 교체를 자동 적용하지 않는다.

근거: [전체 비교표](전사모델비교.md), [입력·설정 manifest](../../work/asr-matrix-20261005/manifest.json), [72회 상태](../../work/asr-matrix-20261005/status.json), [정량 집계](../../work/asr-matrix-20261005/analysis/summary.json), [35사례 의미 검토](../../work/asr-matrix-20261005/semantic-results.json), [보조 지연](../../work/asr-matrix-20261005/early-caption-analysis.json), [추가2 지연 독립 검수](../../work/asr-matrix-20261005/sample2-latency-audit.json), [최종 판단](../../work/asr-matrix-20261005/final-judgment.json), [보존·완료 검증](../../work/asr-matrix-20261005/final-verification.json). 각 `runs/<실행ID>/`의 `run-report.json`·`stream-events.jsonl`·`runtime-calls.jsonl`·`provider-events.jsonl`에서 실제 호출·원문·한국어·시각을 확인할 수 있다.

### 17.5 전체222개 대사의 한국어 내용 적합도 — 후속 요청으로 별도 평가

사용자가 원문 오류율과 **번역된 내용이 원문의 뜻과 얼마나 일치하는지**를 영상별로 구분하도록 요청했다. 저장된72회 결과의 **222개 참고 cue × 18실행 = 3,996개 대사-실행**을 중국어 참고 원문과 최종 한국어로 대조했다. 기존35개 사례의 점수를 전체로 추정한 것이 아니며, 새 ASR·번역 추론이나 Google API 호출은 하지 않았다.

대사마다 핵심 의미 보존 **2점**, 부분 보존 **1점**, 핵심 변경·미전달 **0점**을 부여하고100점으로 환산했다. 각 영상은 해당 조합3회 평균, 합계는 **총점/(2×222×3)×100**이다. 모든 cue는 길이와 무관하게 같은 가중치다. ASR 오류와 번역 오류를 포함한 최종 한국어의 내용 평가이며 번역 모델만의 독립 점수가 아니다.

- 문장 분절이 달라도 인접한 최종 한국어에서 뜻이 이어지면 인정했다. 말투·의역·동일인명 음역 차이만으로 감점하지 않았다.
- 최종 번역 실패는 이전 잠정 결과로 성공 처리하지 않았다. 실제 반복 발화는 같은 문구의 다른 시점 발화로 구제하지 않았다.
- 각 cue에 점수·실제 최종 caption 인덱스·근거·확신도를 남겼다. 18실행의 전체 원문/한국어/상태/정렬 payload가 같은 경우만 해시로 동일성을 확인했다. 다르게 나온 반복은 변경 문구와 문맥을 읽었다.
- 기존 영상의 주어 생략·인명·관계 분절 등 경계6사례는 다른 두 AI 검토자가 추가 확인했으며 점수를 유지했다. 전체3,996행을 복수 원어민이 검수했다는 뜻이 아니다.

| 번역 모델 | Qwen | Whisper | Gemini |
|---|---:|---:|---:|
| HY-MT2-7B | 86.1 | 70.1 | 72.7 |
| MiLMMT-46-12B | 84.9 | 66.9 | 71.2 |

이는 **이번 참고 SRT에 대한 AI 의미 평가**이며 공인 번역 정확도나 원어민 패널 점수가 아니다. 참고 자막의 오류와 평가자의 해석에 영향을 받는다. 특히 짧은 대명사·인명·감탄사·미완절에는 낮은/중간 확신도 판정이 있으며 작은 점수 차이를 확정적 우열로 확대하지 않는다. 유창성·화면 지연·SRT 밖 추가 출력 전체를 한 번에 평가한 사용성 점수도 아니다. Qwen+HY의 유지 판단은 뒷받침하지만, 샘플1·3의 Qwen에서는 MiLMMT가 근소하게 높았으므로 HY가 모든 영상에서 더 낫다고 설명하지 않는다.

근거: [평가 기준](../../work/asr-matrix-20261005/adequacy/rubric.json), [전체·영상별·3회별 집계](../../work/asr-matrix-20261005/adequacy/summary.json), [검증·집계 코드](../../work/asr-matrix-20261005/adequacy/aggregate.py), [경계 사례 추가 검토](../../work/asr-matrix-20261005/adequacy/calibration-review.json), [기존 영상 판정](../../work/asr-matrix-20261005/adequacy/legacy-scores.json), [샘플1 판정](../../work/asr-matrix-20261005/adequacy/sample1-scores.json), [샘플2 판정](../../work/asr-matrix-20261005/adequacy/sample2-scores.json), [샘플3 판정](../../work/asr-matrix-20261005/adequacy/sample3-scores.json). 같은 폴더의 `*-evidence.md/json`에서 참고 원문과 최종 한국어 전체·반복별 차이를 확인할 수 있다.

## 18. 입력·번역 언어 선택과 PC 마이크 입력 (2026-10-05)

### 18.1 구현

- 입력은 기존 `auto/en/zh/ja` 선택과 인덱스를 유지하고 `ko`를 추가했다. 번역 언어는 입력 선택 바로 아래 `ko/en/zh/ja`이며 초기값은 `ko`다. 설정에 새 항목이 없으면 한국어로 열고, 기존 모델·입력 언어 설정은 보존한다.
- `target_language`를 설정, HTTP 번역 요청, WebSocket 시작, 번역 프로필, 출력 문자 검사, 자막 이벤트와 최근 100개 회전 로그에 연결했다. 설정/요청에서 생략하면 저장값, 명시한 null/빈 문자열이면 `ko`다. 출력의 `auto`는 거부한다. 실행 중에는 입력·번역 언어를 잠그며, 세션 시작 시 목표를 복사해 대기 중인 문장과 잠정 교정도 같은 언어로 처리한다.
- HY-MT2·MiLMMT·TranslateGemma·일반 채팅 번역 프로필 모두 목표 언어를 받는다. 한국어 기본 프롬프트·샘플러는 유지한다. HY의 한국어 용어집은 한국어 출력에만 넣는다. 명시/ASR 확정 원문 코드와 목표가 같으면 원문을 그대로 표시한다. 문자 휴리스틱만으로 영어·중국어·일본어 동일 언어를 추측해 번역을 생략하지 않는다. 한자만 있는 일본어/중국어 구분이나 의미 정확성을 문자 검사로 보장하지 않는다.
- Qwen·Whisper의 한국어 입력과 Gemini `ko-KR` 힌트를 연결했다. Gemini를 사용해도 번역 언어는 별개이며 번역은 로컬이다.
- 오디오 소스에 Windows의 활성 녹음 입력장치를 `마이크 · 장치 이름`으로 추가했다. 브라우저→시스템 전체→마이크 순서다. 마이크는 창 핸들이 아닌 안정된 WASAPI endpoint ID로 선택한다. 공유 모드 캡처에서 Loopback을 사용하지 않고, 기존 PCM 변환기와 16 kHz mono PCM16/100 ms 전송을 사용한다.
- 장치 이름/목록 순서가 바뀌어도 같은 ID를 유지한다. 선택 장치가 사라지면 선택을 비우며 다른 마이크·브라우저·시스템 소리로 자동 변경하지 않는다. 기본 소스 선택은 첫 열거에만 한다. 잘못된 ID·출력 장치·마이크 접근 실패는 오류로 알린다. 마이크에는 브라우저 창이 없으므로 기존 모니터 오버레이 동작을 사용한다.

### 18.2 확인한 결과와 한계

- 엔진 전체 자동 검사: **1,199개 통과, 1개 건너뜀**. 설정/요청 생략·빈값·오류, 세션 목표 고정, caption/history 언어, 한국어 ASR 인자, 네 번역 프로필×네 목표, 기존 한국어 프롬프트 회귀를 포함한다. 이전 테스트의 가짜 번역기에도 새 keyword 계약을 반영했다. 향후 회귀를 위해 출력 프로필 검사와 한국어 기준 프롬프트는 `engine/tests/`에 포함했다.
- 설치된 HY-MT2와 MiLMMT에서 **24개 실제 번역 방향 + 8개 동일 언어 통과 = 32개 기능 검사 통과**. 선택 언어 출력 및 설정/기존 번역 로그 보존을 확인했다. 이것은 정밀 번역 정확도 평가가 아니다. MiLMMT 영어→일본어의 ‘만나요→기다립시다’, HY 일부 결과의 ‘전화→연락’ 의미 차이는 남는다.
- 현재 PC에서 `Analogue 1 + 2(Focusrite USB Audio)` 입력 1개를 열거했다. 실제 2초 캡처에서 60,800 bytes를 수신했고 패킷은 모두 3,200 bytes였다. 중지 후 추가 패킷 없음, 재시작, STA/MTA 열거, 잘못된 ID 후 정리, 출력 장치 ID 거부, 기존 샘플레이트/PCM 변환 회귀가 통과했다. 캡처 오디오는 저장하거나 모델/API로 보내지 않았다. 실제 발화 인식 정확도, 물리적 탈착, Windows 권한 차단 상황까지 실험한 것은 아니다.
- 최종 WPF Release 빌드와 오프라인 앱 smoke를 통과했다. 입력 5종×출력 4종 설정 복원 20조합, 기본 한국어, 실행/준비 중 잠금, 마이크 ID 유지 및 제거된 선택의 자동 대체 방지를 검사했다. 생성된 기존 화면 검증 이미지에서 번역 언어가 입력 언어 아래에 배치되는 것도 확인했다. 화면 지연 계측 기능은 추가하지 않았다.
- 새 한국어 Gemini 실제 API 호출, 삭제된 번역 모델의 재다운로드, 기존 72회 비교의 재실행은 하지 않았다. 사용자 요청에 따라 C드라이브 작업 앱만 갱신했고 E드라이브/배포본은 복사하지 않았다. 모델 가중치를 복제하지 않았다.

근거: [전체 엔진 검사](../../work/output-language-20261005/engine-tests.txt), [실제 모델 출력](../../work/output-language-20261005/model-smoke.json), [앱 설정 검사](../../work/output-language-20261005/ui-smoke-final/language-selection-check.json), [최종 빌드](../../work/output-language-20261005/build.txt), [마이크 구현·검사 기록](../../work/microphone-input-20261005/audio-tests/capture-review.txt), [네이티브 캡처](../../work/microphone-input-20261005/audio-tests/native-results.txt), [PCM 변환 회귀](../../work/microphone-input-20261005/audio-tests/converter-results.txt).

## 19. 번역 기록 삭제 버튼 (2026-10-05)

- ‘완전히 종료’ 바로 왼쪽에 **번역 기록 삭제** 버튼을 추가했다. 인증된 네이티브 API `POST /v1/logs/clear`로 앱 `logs` 폴더의 직속 일반 파일을 삭제한다. caption-history.json, llama.log, 해당 폴더의 과거 로그/임시 파일이 대상이며 하위 폴더·링크를 따라가지 않는다. 설정·모델은 삭제하지 않는다.
- 모든 파일 삭제 성공이면 화면의 최근 자막·진행 기록·오버레이도 비우고 완료 파일 수를 알린다. 부분 실패는 삭제 파일 수와 실패 이름을 알리며 성공으로 표시하지 않는다. 실행 중 세션/모델은 중단하지 않는다. 이후 새 자막/엔진 출력은 파일을 다시 생성한다.
- SessionManager의 history 작업 사슬에 삭제 작업을 넣어 삭제 전 큐의 기록→삭제→새 기록 순서를 보장한다. CaptionHistory의 메모리 행도 비운다. 기존 최대 1,000-ID 기억을 유지해 삭제된 최근 caption의 늦은 revision 재기록을 막는다. 영구적인 ID 묘지나 모든 과거 ID의 무기한 차단은 아니다.
- Windows에서 자식 프로세스가 llama.log 핸들을 계속 잡는 문제 때문에 stdout을 PIPE로 받고 별도 스레드가 최대 8KB 청크로 소비한다. 동일 log_lock 안에서 짧게 파일을 열고 닫는다. 삭제 중 잠금/디스크 실패에는 해당 로그 청크를 버리고 소비를 계속한다. 프로세스 종료 후 bounded join으로 파이프/스레드를 정리한다. 인증키가 청크 경계를 넘더라도 전체 키를 로그에 남기지 않는다.
- 엔진 전체 **1,216개 통과, 3개 건너뜀**. 삭제 범위·부분 실패·실행 중 세션 보존·메모리 이력 초기화·동시 큐 기록·인증/Origin 차단을 임시 폴더에서 확인했다. 실제 CPU 자식 프로세스로 실행 중 파일 삭제와 다음 출력 재생성, 큰 출력 소비, idle pipe 종료, 디스크 실패를 확인했다. 새 링크 검사의 2개 skip은 Windows 심볼릭 링크 생성 권한 부재이며 하드링크 검사와 기존 나머지 테스트는 실행했다.
- Release 빌드와 WPF 오프라인 smoke 성공. 버튼 위치·이름, 실행/연결/중복클릭/종료 상태, 합성 삭제 결과 처리를 확인했다. 실제 사용자의 로그는 삭제하지 않았으며 E드라이브·배포본으로 복사하지 않았다.

근거: [전체 엔진 검사](../../work/clear-logs-20261005/engine-tests.txt), [삭제 집중 검사](../../work/clear-logs-20261005/deletion-tests.txt), [WPF 검사](../../work/clear-logs-20261005/ui-smoke/clear-logs-ui-check.json), [빌드](../../work/clear-logs-20261005/build.txt). 재현용 테스트는 `engine/tests/test_clear_logs.py`, `test_runtime_logging.py`에 포함했다.

## 20. 음성 확정 방식 표시 이름 변경 (2026-10-05)

- 사용자 요청으로 ‘빠른 표시 (기본)’을 **기본**, ‘안정 확정’을 **반복확인(느림)**으로 변경했다. ‘안정 확정’이 정확도를 보장하는 이름처럼 보이는 문제를 줄이기 위한 표시 문구 변경이다.
- 설명·호환 오류 안내·문장 경계 재확인 안내·사용법의 이름을 맞췄다. 설명은 여러 인식 결과를 비교하며 지연과 인식 오류가 남을 수 있다고 명시한다.
- 내부 설정값 `legacy`/`stable`, 기존 선택값, 실제 인식·번역 알고리즘은 변경하지 않았다. 과거 실험 기록의 명칭은 당시 기록으로 남긴다. Release 빌드와 기존 WPF smoke가 통과했다(`work/asr-labels-20261005/`). 모델 추론 시험은 반복하지 않았다.

## 21. 창 닫기 안내 위치 변경 (2026-10-05)

- 창 닫기/트레이 안내를 ‘완전히 종료’ 아래, 기존 상태 영역 상단 20 DIP 여백 안으로 이동해 오른쪽 정렬했다. 같은 Grid 행에 NoWrap 안내를 겹쳐 배치하므로 기존 행높이·상태 영역 margin은 늘리지 않는다. 하단에는 MIT 저작권 문구만 중앙에 남긴다.
- Release 빌드와 기존 WPF smoke 성공. 생성 화면에서 한 줄 안내가 기존 여백에 들어가고 상태 영역과 겹치지 않는 것을 확인했다. 좁은 창에서는 말줄임표와 전체 문구 툴팁을 사용한다.
- 실행 중 앱을 중단하지 않고 검증한 DLL/PDB를 반영했으며 재실행부터 새 배치가 적용된다. 이전 두 파일만 `work/footer-layout-20261005/prior/`에 보관했다. 모델 복제나 E드라이브 갱신은 없다. 근거: `work/footer-layout-20261005/build.txt`, `applied.json`, `ui-smoke/main-window.png`.

## 22. 하단 라이선스 영역 축소 (2026-10-05)

- 사용자 요청으로 라이선스 윗여백 14 DIP를 없애고 창 내부 하단 여백을 26→13 DIP로 줄였다. 글자 크기 11·중앙 정렬·Auto 행높이는 유지해 하단 전체 영역을 약 절반으로 줄이며 글자를 자르지 않는다. 위·좌·우 여백과 종료 안내 위치는 유지한다.
- Release 빌드·기존 WPF smoke 통과, 생성 화면에서 라이선스 잘림 없이 본문 높이가 늘어난 것을 확인했다. 실행 중 앱을 중단하지 않고 DLL/PDB를 갱신했다. 재실행부터 반영된다. 근거: `work/license-height-20261005/build.txt`, `applied.json`, `ui-smoke/main-window.png`.

## 23. JA-KO-VN 일본어 전용 모델 연동과 JSON 자막 수정 (2026-10-05)

### 23.1 원인과 적용

- 사용자가 `models/translation/ja-ko-vn-12b-v2-Q4_K_M.gguf`를 직접 넣고 선택했다. 기존 앱은 파일을 목록에 표시하고 로드할 수 있었지만 전용 번역 프로필이 없어 일반 채팅 방식으로 처리했다. `source_language`, `target_language`, `context`, `current_text`를 담은 JSON이 user 원문으로 전송되었고, 이 모델은 형식을 유지하며 JSON 안의 값과 이전 문장까지 번역했다. 한국어 포함 여부만 확인하던 출력 검사도 해당 응답을 통과시켰다. GGUF 로딩 호환성과 번역 입력 형식 호환성은 별개다. 모든 전용 번역 모델에 대해 ‘파일만 넣으면 된다’고 안내하지 않는다.
- 파일명에서 `ja-ko-vn-12b-v2`를 구분하는 전용 프로필을 추가했다. 부모 폴더나 다른 v1/7B 모델에는 적용하지 않는다. 설치 GGUF의 `general.name`에는 v1 표기가 남아 있으므로 이 값만으로 버전을 구분하지 않는다.
- 실제 설치 파일의 tokenizer.chat_template을 메타데이터만 읽어 확인했다. 고정된 일한 번역 system 턴, 현재 일본어 원문 user 턴, model 시작 토큰을 `/completion`에 전달한다. JSON, 이전 자막, 범용 번역 지시, 사고 모드 옵션을 넣지 않는다. 원문의 특수 제어 토큰 표기만 전각 괄호로 무력화한다. `add_bos_token=true`이므로 BOS를 중복 삽입하지 않고 `<end_of_turn>`/`<eos>`로 종료한다. CPU에서 내장 Jinja 렌더와 프롬프트의 정확한 일치를 확인했다.
- 서버는 `--jinja --ubatch-size 128`을 사용한다. greedy(`temperature=0`, `top_k=1`, `top_p=1`, `min_p=0`)와 repetition penalty 1.05는 현재 앱 선택이다. 제작자 카드의 `Top_k=0.95`는 정수 top_k 계약과 맞지 않으므로 그대로 사용하거나 top_p로 임의 해석하지 않았다. 출력 한도는 320 tokens이며 빈 결과·잘림·언어 실패는 기존 문장별 복구 방식으로 처리하고 같은 요청을 반복하지 않는다.
- 준비 단계에서 일본어/자동 입력→한국어 외 설정을 안내하고 모델 교체 전에 거절한다. 자동 인식 중 명시된 다른 언어의 문장이 들어오면 `translation_language` 품질 오류로 처리하여 세션 전체를 중단하지 않는다. 한자만 있는 일본어의 자동 입력은 문자 추정만으로 중국어라고 단정해 거절하지 않는다. 직접 번역의 기존 동일 언어 통과 동작은 유지한다.
- 출력 검사에 JSON object의 `current_text`와 요청 메타데이터 키가 함께 나타나는지 확인하는 좁은 검사를 추가했다. 해당 JSON이나 JSON 코드펜스를 정상 자막으로 표시하지 않으며 current_text만 추출해 실패를 숨기지 않는다. 일반 JSON 문장 전체를 금지한 것은 아니고, 설명 접두사 등이 붙은 모든 변형까지 검출하는 일반 구조 검사는 아니다.
- 앱에서 이 모델을 선택하거나 저장 설정을 복원하면 기본 번역 테스트 문구를 일본어로 바꾼다. 다른 모델로 바꾸면 기본 영어 문구로 돌아간다. 직접 입력한 문구·빈 입력·언어 선택은 변경하지 않는다. 기존 모델 설명 자리에 일본어→한국어 전용 안내를 넣었고 기존 사용법에 다운로드 링크와 전용 연동 조건을 기록했다.

### 23.2 검증과 한계

- 동일하게 로드된 실제 GGUF에서 기존 일반 프로필을 한 번 재현했다. `さらにこの学園には階級制度が存在する。`가 이전에는 JSON 전체로 출력되었고, 전용 프로필에서는 `게다가 이 학원에는 계급제도가 존재한다。`만 출력되었다. 기록 원문·인사·부정문·인명·한자만 있는 원문 등 총 8문장의 전용 번역이 JSON 없이 한국어로 반환되었고, 지원하지 않는 두 언어 조합은 추론 요청 전에 거절되었다.
- 위 8문장의 요청 시간은 약 0.126~0.566초였다. 번역 HTTP 요청만 측정한 값이며 음성 구간 대기·ASR·화면 표시가 포함된 지연이나 HY-MT2 대비 속도 순위가 아니다. 첫 기존 JSON 재현은 약 1.374초였지만 한 번의 길이·캐시 조건이 다른 관찰이므로 일반 속도 개선 수치로 사용하지 않는다.
- 형식 수정은 의미 정확도 개선을 보장하지 않는다. `やばいやばいやばい。`는 `야바이 야바이 야바이。`로 음차했고, 잘못 인식된 원문은 어색한 의미가 남았다. `あの映画には性的な描写があります。` 한 문장은 거절 없이 번역했지만 모든 성적·민감 표현의 무거절을 검증한 것은 아니다. HY-MT2와 일본어 의미 정확도 비교 평가도 수행하지 않았다.
- 전용 회귀 검사 82개를 포함해 엔진 전체 **1,298개 통과, 3개 건너뜀**. 파일명 경계, 정확 프롬프트, BOS/stop, JSON·문맥 미주입, 언어 제한, 잘림·오류 구분, 요청 메타데이터 누출과 일반 JSON 허용, 기존 번역 프로필 회귀를 확인했다.
- .NET 10 Release publish와 오프라인 WPF smoke 성공. 모델 선택/설정 복원 때 기본 예시 교체, 수동 문구 보존, 언어 선택 보존, 기존 UI 검사가 통과했다. 이 smoke는 합성 화면을 확인하는 기존 기능이며 화면 지연 계측이나 실시간 오디오 캡처를 추가하지 않았다.
- 앱이 자막 세션 없이 대기 중임을 확인하고 기존 모델을 해제한 뒤 DLL/PDB를 반영하여 재실행했다. 실제 앱 `/v1/prepare`에서 Qwen3-ASR와 JA-KO-VN 동시 준비 성공, `/v1/translate` 3문장에서 JSON 없는 한국어 확인, 잘못된 목표 언어에 422 안내를 확인했다. 설정과 번역 기록은 보존했고 모델 복제·추가 다운로드·E드라이브 갱신·클라우드 API 호출은 없었다.

근거: [제작자 모델 카드](https://huggingface.co/hell0ks/ja-ko-vn-12b-v2-gguf), [설치 템플릿 비교](../../work/jakovn-setup-20261005/independent-template-check.json), [기존/수정 실제 출력](../../work/jakovn-setup-20261005/live-model-verification.json), [전체 엔진 검사](../../work/jakovn-setup-20261005/engine-tests.txt), [UI 검사](../../work/jakovn-setup-20261005/ui-smoke/translation-test-defaults-check.json), [빌드](../../work/jakovn-setup-20261005/build.txt), [적용 기록](../../work/jakovn-setup-20261005/applied.json), [재실행 후 실제 API 검사](../../work/jakovn-setup-20261005/app-api-verification.json).


## 24. UI 다국어 선택 (2026-10-05)

### 24.1 구현

- `완전히 종료` 버튼 위에 `Language`와 `한국어 · English · 中文 · 日本語` 콤보박스를 추가했다. 상단에 새 행을 넣지 않고 22 DIP 선택기 + 3 DIP 간격 + 28 DIP 종료 버튼으로 배치했다. 상단 전체 행은 기존 57.53 DIP를 유지한다.
- `Services/UiText.cs`와 내장 JSON 카탈로그 4개에 고유 문구 297개를 구성했다. XAML은 DynamicResource, 런타임 안내문은 단방향 바인딩, 새 로그·대화상자·장치 오류는 템플릿 번역을 사용한다. 문자열 자리에 모델·장치 이름 등 인수를 전달하며 실제 음성 원문·번역문을 번역 사전으로 치환하지 않는다.
- 메뉴, 버튼, 설명, 도구 설명, 입력/번역 언어 목록, 주요 상태·오류 안내, 오디오 소스 접두사, 트레이 메뉴, 오버레이 편집 안내가 UI 언어를 따른다. 언어 선택기 자체는 언제나 각 언어의 원어 이름으로 표시한다. 기존 로그는 생성 당시 언어를 유지하며 외부 라이브러리·미등록 동적 진단 메시지는 원문으로 남을 수 있다.
- 화면 언어는 `config/ui-settings.json`의 `UiLanguage`에 별도로 저장한다. 기본값과 알 수 없는 값의 대체값은 한국어다. 엔진 연결 전에 복원하며 연결 실패 상태에서도 저장한다. 입력 언어/번역 언어의 내부 `ko/en/zh/ja/auto` 값은 UI 언어 변경과 독립적이다.
- UI 언어는 준비 중·자막 실행 중에도 전환할 수 있다. 언어 전환 경로에서 엔진 요청, 오디오 소스 재선택, 모델 재준비, 오버레이 위치 변경을 호출하지 않는다. 인식 원문 미리보기, 자막, 번역 테스트 결과, 직접 입력한 테스트 문구와 힌트를 보존한다.
- 서비스가 이미 번역한 고정 오류 문구도 UI 바인딩 시 유일한 원문 키로 복원해 이후 언어 변경에 맞춘다. 이 역조회는 UI 바인딩에만 적용하며 사용자 자막에 적용하지 않는다. 모든 동적 외부 오류의 재번역을 보장하는 구조는 아니다.

### 24.2 검증과 적용

- .NET 10 Release publish 성공, 경고·오류 0. Python 엔진이나 모델 추론 로직은 변경하지 않았다.
- 실제 WPF 컨트롤을 사용하는 오프라인 검사에서 네 UI 언어 각각 1280/1050 DIP 창 너비를 확인했다. 전환 전후 상단 행 높이는 모두 **57.5333 DIP**, 언어 콤보박스 높이는 **22 DIP**였다. 버튼 글자 너비와 드롭다운 동작을 확인했고, 중국어 모델 추가 버튼의 줄바꿈은 해당 버튼의 글자 크기·좌우 여백 조정으로 해소했다.
- 합성 실행 상태에서 입력 언어 일본어·번역 언어 영어를 고정한 채 네 UI 언어로 전환했다. 입력/번역 목록의 표시 이름만 바뀌고 코드·실행 상태·실제 자막·미리보기·사용자 문구·번역 결과·힌트가 유지됨을 확인했다.
- 네 언어별 설정 저장→파일 읽기→화면 복원, 알 수 없는 언어의 한국어 대체, 이미 영어로 발생한 서비스 오류의 일본어 재표시, 기존 입력/번역 선택·기록 삭제·일본어 전용 모델 안내 회귀 검사를 통과했다.
- 검사에서는 실제 모델·오디오·클라우드 API를 사용하지 않았다. 화면 지연 측정 기능을 추가한 것이 아니라 언어 선택·배치·상태 보존을 확인한 것이다. 앱이 종료된 상태에서 현재 앱 폴더의 DLL/PDB만 반영했다. 모델 파일과 E드라이브의 배포본은 복사하지 않았다.

근거: [검사 소스](../../work/ui-localization-20261005/probe/Program.cs), [기존 행 높이](../../work/ui-localization-20261005/baseline/baseline.json), [다국어 검사 결과](../../work/ui-localization-20261005/checks-verified/results.json), [적용 기록](../../work/ui-localization-20261005/applied.json).

### 24.3 상단 한 줄 배치와 소개 문구 수정 (2026-10-05)

- 사용자 확인 후 언어 선택기를 종료 버튼 위에 쌓는 배치를 폐기했다. 왼쪽 `LiveSubtitle` 제목 바로 옆에 번역 언어·항상 로컬 안내를 옮기고, 오른쪽은 `Language → 번역 기록 삭제 → 완전히 종료` 순서로 한 줄 배치했다. 세 컨트롤은 동일한 Y 좌표·36 DIP 높이다. 상단 전체 행 높이는 기존 57.53 DIP를 유지한다.
- 기존 “방송의 목소리를, 내 언어로.” 대신 **“음성 인식·번역을 위한 실시간 자막 오버레이”**를 사용한다. 네 UI 언어의 문구와 README 첫 소개, 사용법의 선택기 위치 안내를 반영했다.
- 기존 WPF 검사에 한 줄 순서·같은 높이·제목 옆 배지·겹침 검사를 반영했다. 네 언어에서 1050/1280 DIP 너비 검사가 통과했고 한국어·영어 렌더링을 확인했다. 기존 언어 전환·설정·자막 보존 검사도 통과했다. 앱이 닫힌 상태에서 DLL/PDB만 갱신했다.
- 근거: [검사 결과](../../work/ui-header-row-20261005/checks/results.json), [적용 기록](../../work/ui-header-row-20261005/applied.json).

## 25. 사용자 매뉴얼 4개 언어 제공 (2026-10-05)

- 사용자 안내 폴더를 `0_UserManual(사용자매뉴얼)`로 변경했다. `사용자매뉴얼.md`(한국어), `UserManual.md`(영어), `用户手册.md`(중국어), `ユーザーマニュアル.md`(일본어)에 전체 사용 절차를 제공한다. 각 문서의 버튼 이름은 해당 UI 언어 카탈로그에 맞췄다.
- README·소스 공개 안내·모델 다운로드 안내와 .gitignore의 허용 경로를 함께 수정했다. 각 매뉴얼에서 다른 언어로 이동할 수 있다. 제공되는 Qwen·Whisper 부속 파일과 별도 다운로드 가중치의 차이도 반영했다.
- 네 문서의 6개 섹션, 6개 외부 모델 주소, 파일명·경로·수치·예시를 비교했다. 문서 링크 36개와 제목 앵커 검사, 구 경로 잔존 검사 및 변경분 공백 검사가 통과했다. 문서 변경이므로 앱 빌드·모델 추론은 다시 실행하지 않았다.
- GitHub main에 `840106f12767b4666a06c17ca1240744e134c051`로 커밋·푸시하고 원격 해시 일치를 확인했다. E드라이브의 소스·배포본은 변경하지 않았다.
- 근거: [매뉴얼 검사 결과](../../work/github-publication-20261005/manual-validation.json).

### 25.1 README 사용 조건·사용처·지연 안내

- 한국어·영어 소개 첫 문단 다음에 사용 조건과 사용처를 추가했다. 사용자 제시 조건인 여유 RAM 10GB와 GPU 필수, 테스트 GPU RTX 4080 16GB를 명시했다. 10GB는 전체 설치 메모리나 VRAM 수치가 아니며, 모든 모델의 최소 사양을 새로 실측한 결과도 아니다. 모델에 따른 RAM·VRAM 차이를 함께 안내한다.
- 인터넷 방송·YouTube·마이크 외에 시스템 오디오를 통한 영상·강의·팟캐스트, 입력/번역 언어가 같을 때의 원문 자막을 추가했다. 해당 경로는 기존 소스로 확인했으며 OBS 자동 연동·송출을 보장하지 않는다.
- 사용자가 요청한 약 5초 지연을 GitHub 경고 블록으로 강조했다. 모델·PC·발화·동시 프로그램에 따라 달라지는 대략적 안내이며 지연 고정값 또는 새로운 벤치마크 결과로 해석하지 않는다. 코드 변경·모델 추론·앱 빌드는 수행하지 않았다.

## 26. v1.0.0 로컬 실행본 준비 (2026-10-05)

- 사용자가 GitHub 업로드를 보류하도록 지시했다. 이 작업에서는 커밋·푸시·태그·릴리스를 생성하지 않았다. E드라이브 소스·배포본도 변경하지 않았다.
- 버전은 사용자 지정 `v1.0.0`이다. C# 프로젝트에 Version/InformationalVersion 1.0.0, AssemblyVersion/FileVersion 1.0.0.0을 명시하고 패키지 실행 파일의 메타데이터를 확인했다.
- 모델 가중치를 전부 빼도 실행 환경만 포함한 묶음은 약 8.4GB이다. 사용자는 PyTorch·CUDA·NVIDIA·llama.cpp 라이브러리는 포함하도록 확정했다. 필수 Silero VAD 자산은 유지하며, 대형 ASR/번역 가중치·기존 개인 설정/키/로그는 제외했다. 기본 설정은 Qwen3-ASR + HY-MT2, auto→ko, legacy, 힌트 없음이다.
- 번역 다운로드 안내는 HY-MT2-7B Q6_K, Xiaomi MiLMMT-46-12B-v1.0 i1-Q4_K_M, TranslateGemma 12B Q4_K_M 세 개로 확정했다. 네 언어 매뉴얼, translation 폴더 다운로드 문서, 로컬 릴리스 설명에 반영했다. 모델 파일 자체는 삭제하거나 복제하지 않았다.
- 원본 복사 해시·개인 정보 제외 검사, 패키지 Python의 엔진/인증/모델 누락 안내, 현재 PC의 PyTorch/CTranslate2 CUDA 인식, VAD·llama.cpp 시작, 실제 패키지 EXE의 오프라인 UI 검사에 통과했다. 이번 준비에서 실시간 ASR/번역/Gemini를 다시 실행하지 않았고 새 PC 호환성도 아직 검증하지 않았다.
- 외부 고지 원문 13개를 추가 확보했다. 공개 전에는 FFmpeg/코덱 대응 소스와 조건, Intel OpenMP 고지, nvperf_host.dll 재배포 조건을 마무리해야 한다. 현재 묶음은 로컬 검토본이며 공개 준비 완료로 취급하지 않는다.
- 작업 위치: [로컬 준비 상태](../../work/release-v1.0.0-20261005/준비상태.md), [패키지 검증](../../work/release-v1.0.0-20261005/package-verification.json), [엔진 검사](../../work/release-v1.0.0-20261005/smoke-result.json), [라이선스 점검](../../work/release-v1.0.0-20261005/license-audit.md), [릴리스 설명](../../work/release-v1.0.0-20261005/RELEASE_NOTES.md).

### 26.1 하단 버전 표시와 버전 변경 시 필수 작업

- 저작권 표시 행 오른쪽 끝에 `v1.0.0`을 표시했다. 저작권은 가로 중앙을 유지하고, 기존 하단 행 높이 14.63 DIP를 유지한다. 네 UI 언어와 1050/1280 DIP 너비에서 정렬·겹침·기존 UI 기능 검사가 통과했다.
- XAML에 버전 문자열을 직접 쓰지 않는다. MainWindow가 자신의 앱 어셈블리 `AssemblyInformationalVersion`에서 읽고 `v`를 붙인다. 빌드 메타데이터의 `+...` 부분은 표시하지 않는다. 테스트 실행기의 EntryAssembly를 사용하면 잘못된 버전이 나올 수 있으므로 앱 어셈블리를 유지한다.
- **버전 변경 시 반드시** `src/LiveSubtitle.App/LiveSubtitle.App.csproj`의 `Version`과 `InformationalVersion`을 새 버전으로 바꾸고, `AssemblyVersion`과 `FileVersion`도 대응하는 4자리 값으로 갱신한다. 예: `1.1.0` 및 `1.1.0.0`. 다시 빌드하면 화면 버전도 자동으로 바뀐다.
- 실행 중인 앱 파일을 덮어쓰지 말고 종료 상태에서 새 EXE/DLL을 반영한다. 소스만 바꾸고 이전 DLL이나 압축파일을 배포하지 않도록 주의한다.
- 릴리스 제목·태그·압축파일명·BUILD_INFO.json·릴리스 설명의 버전도 함께 변경하고 압축파일 및 SHA-256을 다시 생성한다. 다음 릴리스 준비 스크립트의 고정 버전 문자열도 확인한다. 현재 사용자의 업로드 보류 지시가 해제되기 전에는 커밋·푸시·태그·릴리스를 만들지 않는다.
- 근거: [수정 전 행 높이](../../work/footer-version-20261005/baseline/baseline.json), [네 언어 검사 결과](../../work/footer-version-20261005/checks/results.json).

### 26.2 릴리스 라이선스 재확인 (2026-10-05)

- 실제 게시되는 NETCore/WindowsDesktop 10.0.12 NuGet 런타임 팩은 MIT다. SDK 루트의 .NET Library 약관을 런타임 팩에 적용하던 이전 추론을 정정했다. 이후 점검은 SDK 약관 대신 실제 팩 원문·nuspec·게시 DLL 해시를 사용한다.
- Intel OpenMP의 두 DLL은 Intel OpenMP 2025.3.2 Windows wheel과, `nvperf_host.dll`은 공식 CUPTI 12.8.90과 해시가 일치했다. 원문 EULA와 고지를 확보했다. 출처 특정과 명시적 재배포 권한 확인은 구분하며, 후자는 여전히 확인할 내용이 남았다.
- Tokenizers 0.23.2·FlatBuffers 25.12.19의 누락 원문을 공식 태그에서 확보했다. 기존 libsndfile 1.2.2 DLL도 고정 upstream과 해시가 일치하며, LGPL/정적 코덱의 대응 소스 제공 항목을 추가했다.
- FFmpeg 런타임의 LGPL 문자열이나 upstream의 암묵적 코덱 예외 언급만으로 LiveSubtitle의 재배포 조건이 충족됐다고 판단하지 않는다. 필요 없는 GPL 코덱을 제외한 오디오용 빌드와 소스 묶음을 후속 후보로 기록했다. 실제 교체·빌드·추론은 하지 않았다.
- 앱 코드·실행본·모델·기존 압축파일·E드라이브를 변경하지 않았고 업로드도 하지 않았다. 새 원문은 조사 폴더에 확보한 상태이므로 고지 반영과 최종 압축 갱신은 후속 작업이다.
- 상세 근거: [라이선스 재확인](../../work/release-v1.0.0-20261005/license-review-20261005/README.md). 같은 바이너리의 출처 탐색을 반복하기 전에 저장한 일치 해시와 원문을 확인한다.

### 26.3 재배포 근거 및 디코더 제외 후보 검증 (2026-10-05)

- Intel은 `intelopenmp.redist.win 2025.3.2.833` 공식 NuGet에서 현 OpenMP 두 DLL과 동일한 바이너리를 ISSL October 2022로 배포한다. 정확한 패키지 해시와 DLL 일치를 확인했다. 참조된 oneTBB 고지도 DLL의 2022.2.0 문자열과 GUID가 일치하는 Intel PDB로 추적하여 확보했다. 이전 PyPI EULA의 redist.txt 탐색만 반복하지 않는다.
- PyAV/FFmpeg 및 SoundFile/libsndfile을 시험 환경에서 제외했다. MIT faster-whisper 1.2.1 `audio.py`의 PyAV import를 파일 디코딩 시점으로 지연하는 후보 패치와 선택 의존성 패치를 준비했다. 제품 입력은 PCM 배열이므로 외부 파일 디코더를 호출하지 않는다. 원래 runtime 및 배포 압축파일에는 아직 적용하지 않았다.
- `nvperf_host.dll`은 266개 PE의 필수/지연 import 목록에 없었다. 이를 뺀 독립 torch 복사본에서 CUDA 행렬 곱·cuDNN Conv1d를 확인했다. 다른 CUPTI/CUDA DLL은 유지했다.
- 원래 환경과 세 구성요소를 제외한 통합 후보 환경에서 `Runtime._load_asr()`/`transcribe()`를 사용해 중국어 PCM 4.2039375초를 Qwen/Whisper 각각 1회씩, 총 4회 실제 전사했다. 모델별 전사 결과·언어가 원래 환경과 동일하며 후보에는 av/soundfile import와 nvperf/libsndfile/av.libs/x264/x265 DLL 로딩이 없었다. 모델 파일을 복제하지 않았다.
- 첫 Qwen 기준 실행은 JSON 결과 저장 후 cp949 콘솔 출력만 실패하여 시험 스크립트 stdout을 UTF-8로 수정했다. 저장 결과를 검사했으며 기준 추론을 다시 수행하지 않았다. 일회성 초기화 포함 시간은 성능 비교로 사용하지 않는다.
- 한 개의 짧은 표본을 통한 제외 가능성 검증이다. 새 PC·장시간 실행·실시간 캡처/번역/오버레이·모든 ASR 프로파일 시험은 아니며 전체 품질 보증도 아니다.
- 실제 반영 시 기존 Silero v6 `vad.py` 변경을 보존하고, 수정된 faster-whisper의 재현 패치·버전·선택 av 의존성·배포 요구 목록을 함께 정리해야 한다. 고지·출처를 갱신한 뒤 최종 실행본 검사와 압축 재생성이 필요하다. 기존 앱/압축파일/E드라이브 및 GitHub는 바꾸지 않았다.
- 근거: [후속 확인 결과](../../work/release-v1.0.0-20261005/license-followup-20261005/README.md), [실제 전사 비교](../../work/release-v1.0.0-20261005/license-followup-20261005/asr-comparison.json).


### 26.4 최종 소스·배포본 정리와 실제 추론 확인 (2026-10-05)

- 소스의 오래된 Qwen3.5-4B/Whisper 기본값을 Qwen3-ASR-1.7B + HY-MT2 Q6_K로 통일했다. auto→ko/legacy/힌트 없음/경계 재확인 꺼짐/브라우저 기본을 유지하며 개인 설정은 보존한다.
- 최신 실행본은 `work/release-v1.0.0-20261005/final/`이다. 소스192개를 SHA-256으로 식별하고 `SOURCE_MANIFEST.json`/`BUILD_INFO.json`에 미커밋 변경을 포함한 기준을 기록했다. 현재 앱 DLL과 배포 DLL도 같다. 기존 release 루트의 package/assets는 과거 검토본이다.
- README에 사용자가 최종 제공한 네 youtu.be 주소와 한국어/영어 오디오 품질 경고를 반영했다. 샘플2/3의 과거 로컬 파일명은 모두 같은 Q9 ID를 담지만 다운로드 메타데이터로 출처가 입증된 것이 아니므로 사용자 교정 주소를 유지했다. 72회 평가 결과를 다른 영상으로 재계산하거나 바꾸지 않았다.
- `engine/dependencies/build_pcm_whisper.py`로 정확한 upstream wheel을 확인하여 `1.2.1+livesubtitle.pcm1`을 재현한다. 선택 PyAV extra/버전/RECORD/원문 MIT를 함께 보존한다. 소스 설치는 일반 pip 명령 전에 이 helper를 사용한다.
- 앞선 VAD 변경 추정 정정: 현재 `vad.py`와 `silero_vad_v6.onnx`는 upstream 1.2.1 wheel과 동일하다. 별도 VAD 패치를 만들거나 구버전으로 교체하지 않는다.
- 최종 배포에서 PyAV/av.libs, SoundFile/libsndfile, nvperf를 제외했다. 다른 CUDA/CUPTI는 유지한다. 실제 작업용 runtime의 선택 패키지를 지우지는 않았다.
- 추가로 찾은 CTranslate2 Intel DLL은 Torch DLL과 다르다. 공식 NuGet2025.3.0.640에서 같은 bytes와 ISSL 고지를 확보했다. Torch용2025.3.2.833과 구분해 공식 사본을 복사했다. 배포 고지22개와 source patch hashes를 유지한다. 과거 Intel 전체가 두 DLL뿐이라는 가정으로 돌아가지 않는다.
- 배포 Python 엔진 테스트1298통과/3skip/4warnings, pip check, 초기API·GPU·VAD·llama·모델누락, UI진단통과. 같은4.2039375초 PCM으로 Qwen/Whisper각1회 준비추론+전사 후 이전출력일치. HY로 두원문 한국어번역확인. 전체 ASR정확도·속도 재평가가 아니며 실제 캡처·동시모델부하·새PC·Gemini재시험은 아니다.
- 최종 22,956파일, 8,316,612,887bytes. 압축 4,252,997,708bytes, 3분할. 압축해제 후 전체파일hash일치. 모델가중치·키·토큰·로그없음. 모델복제·Git게시·E드라이브변경없음.
- 범위와 경고를 포함한 상세 기록: [최종 완료 상태](../../work/release-v1.0.0-20261005/final/완료상태.md). 같은 자료수집이나 제외실험을 반복하기 전에 해당hash·시험결과를 읽는다.


### 26.5 최종 소스 GitHub 반영 (2026-10-05)

- 사용자의 '깃허브에 최종 소스 커밋해' 지시에 따라 main에 `71e33a5c79362e31f4bb68e3effffff985d912fc`를 커밋·푸시했다. GitHub 원격 HEAD와 로컬 HEAD 일치를 확인했다.
- 공개 소스 192개 파일이 최종 배포 manifest와 크기·SHA-256 모두 같음을 확인했다. 변경 파일은 46개다. 모델 가중치·실행 환경·개인 설정·키·로그·비공개 기록은 제외했다.
- 고정 라이선스 원문과 unified diff의 문맥 공백은 임의로 고치지 않았다. 변경 없는 최종 소스이므로 앞선 1,298개 통과 테스트를 반복하지 않았다.
- 로컬 릴리스 초안의 링크를 최종 커밋으로 갱신했다. 태그·릴리스 생성, 배포파일 업로드, E드라이브 복사는 하지 않았다. 기존 압축파일은 그대로이며 SOURCE_COMMIT.json으로 소스 commit과 manifest를 연결했다.
- 근거: [게시 기록](../../work/github-publication-20261005/final-source-publication.json), [커밋](https://github.com/digital-101-Git/LiveSubtitle/commit/71e33a5c79362e31f4bb68e3effffff985d912fc).

### 26.6 다른 PC 기본 구동 사용자 확인 (2026-10-05)

- 사용자가 GPU 없는 노트북에서 "정상 구동 확인 완료"라고 보고했다. 다른 PC의 기본 실행 확인을 완료로 기록한다.
- 해당 노트북의 음성 인식·번역은 시험하지 않았다. 별도 NVIDIA PC의 GPU 추론, 설치 환경 전체의 독립성, 개별 메뉴별 점검까지 완료했다고 해석하지 않는다.
- 로컬 검증 기록과 준비 상태만 갱신했다. 프로그램 소스·배포 압축파일·GitHub 커밋은 변경하지 않았다.
- 근거: [사용자 확인 기록](../../work/release-v1.0.0-20261005/final/external-validation.json).


### 26.7 v1.0.0 정식 릴리스 게시 (2026-10-05)

- 사용자 '릴리즈 생성해' 요청으로 기존 게시 보류를 해제하고, 검증된 소스 커밋 `71e33a5c79362e31f4bb68e3effffff985d912fc`에 v1.0.0 정식 릴리스를 생성했다.
- 초안 상태에서 분할 압축 3개(총 4,252,997,708 bytes)와 SHA256SUMS.txt를 업로드했다. 전송 중 해시 및 GitHub asset digest/size를 모두 확인한 뒤 정식/latest로 게시했다.
- 한국어·영어 릴리스 본문에 추천 Qwen+HY와 선택 Whisper/MiLMMT/TranslateGemma의 다운로드·저장 위치, 3분할 설치법, 드라이버 요건, 약 5초 지연과 음질 경고를 안내했다. 모델 가중치·사용자 비밀·로그는 업로드하지 않았다.
- 공개 API에서 릴리스와 첨부파일, 태그 commit 일치를 확인했다. 프로그램 소스나 압축파일 내부는 바꾸지 않았다. E드라이브 복사도 하지 않았다.
- 릴리스: [https://github.com/digital-101-Git/LiveSubtitle/releases/tag/v1.0.0](https://github.com/digital-101-Git/LiveSubtitle/releases/tag/v1.0.0). 근거: [게시 검증](../../work/release-v1.0.0-20261005/final/github-release.json).

### 26.8 main PR·본인 승인 필수 규칙 (2026-10-05)

- 사용자가 PR 필수에 이어 "승인필수이고 나만 승인 가능하도록" 요청했다. main에 활성 ruleset `24506781`을 만들고 필수 승인 1회 + 코드 소유자 승인으로 강화했다. 전체 파일과 `.github/`의 유일한 CODEOWNER는 `@digital-101-Git`이다.
- 기존 PR 필수·승인 0회 규칙 아래에서 설정 PR #1을 병합해 CODEOWNERS를 main에 먼저 반영한 뒤, 승인 1회 필수 규칙을 적용했다. 설정 커밋은 `aacba6b9704f2ad3d91e1f390167632ea1edd212`이다. `.gitignore`에는 CODEOWNERS만 추가 허용했다.
- 승인 뒤 수정이 올라오면 승인을 취소하고 재승인을 요구한다. main 직접 푸시·강제 푸시·삭제를 차단하고 관리자/소유자 bypass 예외는 없다. 향후 소스 변경은 새 브랜치·PR로 진행한다.
- 다른 사람이 작성한 PR은 소유자 승인 없이는 병합할 수 없다. 공개 저장소의 타인이 리뷰를 작성하는 기능 자체를 금지하는 설정은 아니다. 타인의 승인으로 필수 코드 소유자 승인을 대체할 수 없다.
- GitHub는 작성자의 자기 PR 승인을 허용하지 않는다. 현재 토큰도 소유자 계정이므로 이 토큰으로 작성한 PR은 소유자가 스스로 승인할 수 없다. 이후 임의로 규칙을 낮추거나 우회 예외를 만들지 않는다. 별도 작성자와 소유자 승인 절차 또는 사용자가 명시한 정책 변경이 필요하다.
- GitHub CODEOWNERS 오류 목록이 비어 있고, 활성 main 규칙·필수 승인 수·코드 소유자 조건·bypass 없음이 일치함을 API로 확인했다. 실제 차단을 시험하려는 main 푸시/삭제는 하지 않았다. 앱 실행 코드 및 v1.0.0 태그·배포파일은 그대로다.
- [설정 PR](https://github.com/digital-101-Git/LiveSubtitle/pull/1), [규칙](https://github.com/digital-101-Git/LiveSubtitle/rules/24506781), [검증 기록](../../work/github-rulesets-20261005/owner-policy-verified.json).

### 26.9 최종본 보존 및 임시 산출물 정리 (2026-10-05)

- 사용자 "최종본 제외하고 불필요한거 모두 삭제해라" 지시에 따라 작업 폴더 내부의 이전 배포 package/assets, 압축 해제 검증 사본, 구버전 UI publish, 임시 추론 환경, 설치용 다운로드, .NET bin/obj, Python·컴파일 캐시, 구버전 rollback 실행파일을 삭제했다.
- 171개 파일/폴더 대상으로 34,695,350,671 bytes를 삭제했고 실패는 없었다. 모델 가중치는 삭제하거나 복사하지 않았다. E드라이브·GitHub 릴리스·태그는 변경하지 않았다.
- 현재 앱·소스·모델·runtime·개인 설정·매뉴얼·라이선스·Git 저장소, 최종 `final/package/LiveSubtitle` 및 `final/assets`를 보존했다. 모델/runtime/최종 배포의 56,789개 파일 크기·수정시각과 추적 소스 193개의 SHA-256이 정리 전후 동일함을 확인했다.
- 과거 시도의 고유 소스·시험 결과·연구/라이선스 근거·평가 입력은 재시도 방지를 위해 남겼다. 참조 공개 소스와 원본 시험 영상도 보존했다. .NET SDK·7-Zip·YouTube 도구와 최종 재생성용 Intel 원본 DLL 및 patched wheel은 유지했다.
- 삭제한 `publish`·`fresh-extract`·컴파일 캐시·후보 runtime 경로를 과거 보고서가 가리킬 수 있다. 이는 의도적인 정리다. 과거 검증 실패나 기록 누락으로 오해하지 않는다. 재생성 시 보존한 소스/스크립트로 먼저 빌드·압축 해제·시험환경 준비를 실행한다.
- Junction은 연결 대상에 진입하지 않고 링크만 삭제했다. 최종 package runtime으로 연결된 보존용 inference-state 링크의 대상도 그대로다.
- [정리 목록 및 결과](../../work/cleanup-final-20261005/README.md), [1차 보존 검증](../../work/cleanup-final-20261005/preservation-verification.json), [추가 정리 보존 검증](../../work/cleanup-final-20261005/phase2/preservation-verification.json).

## 27. 정확도·지연 개선 가능성 심층 재검토 (2026-10-06)

- 사용자가 공개 자료와 현재 구현을 최대한 깊게 조사해 개선 불가능 여부를 확인하도록 요청했다. 중국어·영어권 공식 코드·논문·모델·runtime 문서와 기존 실험을 대조했다. 이어 사용자가 실제 시험 후 가치 있는 변경을 적용하도록 승인했다. 연구 결과와 후속 채택 여부는 분리한다.
- 현재 기본판 Qwen24회/1,356호출의 서로 다른226입력을 CPU로 다시 판별했다. 32입력이 whole-wave Silero gate에서 거절되어 빈 결과였으며, gate통과 후 ASR 자체가 빈 결과인 입력은 없었다. 참고자막과 겹친7개 중6개는 앞뒤 창에서 이미 다룬 문구이므로 누락7건이라고 집계하지 않는다.
- sample1-request21(51.68–55.68초)은 gate최대0.34066으로 거절됐지만 동일 Qwen·zh·힌트 없음의 격리 직접 전사에서 `些时段出现，届时。`를 얻었다. 기존 앞창 `…晚些时。`와 다음 `沈小姐。` 사이의 복원 가능 문구다. 모델 자체의 인식 한계와 필터 손실을 구분한다. 겹침 중복·추가 ASR/MT 비용이 있으므로 이 결과만으로 제품 개선이나 속도 비악화를 확정하지 않는다.
- 다른 거절창의 직접 인식은 대부분 `啊/嗯/哎` 등이었다. 전수 음향 검수 없이 모두 환각이라 부르지 않으며 gate전면제거도 하지 않는다. 과거800ms로 VAD state만 조건화하되 과거영역/강제경계200ms overlap/padding/잠정 요청을 제외하는 좁은 후보를 후속 실제 재생 시험으로 넘겼다. 원래 ASR 파형·greedy·모델은 유지한다.
- 기존 Qwen+HY의 nonempty ASR 중앙은 영상별0.33–0.36초, p95 0.66–0.75초다. 최대4초 수집/종료0.5초/MT 대기와 구분한다. 전체 closedwindow 미전달0, ASR·MT 겹침31/1,356호출이라 해당 표본에서 worker수나 GPU경합을 주병목으로 볼 근거는 부족했다. native processor CPU중앙약1.42ms에 불과하여 고정prompt cache 등 미세개선은 추진하지 않는다.
- FireRedASR2-AED는 과거 실제 실패가 아니라 자료상 보류였다. 현재 BF16 수정·PCM배열 입력을 확인해 독립 품질 비교 후보로 올렸다. Qwen3-ASR의 새 대형 로컬 weights는 확인하지 못했고 Qwen-Audio3.0/3.1 API 계열과 구분한다. Voxtral13언어·Moonshine중국어/일본어 새MIT streaming·MOSS영화모델 등은 언어별평가/짧은입력/메모리 조건을 따로 검사했다.
- ONNX/GGUF의 공개 지원은 확인했지만 현재 native와 전처리·유효 길이·언어prompt·encoder문맥이 다른 구현이 있었다. validator 성공 종료만으로 token동등성을 주장하지 않는다. 8초완성block cache는 현4초독립창에 그대로 이득을 주지 않는다. 최신ProgDraft/Whisper-Flash도 target정밀도/beam/지원checkpoint/플랫폼이 달라 즉시 무손실 가속으로 제시하지 않는다.
- 4→3초, 반복확인 기본화, strict neural PCM제거, 이전 동일Nemotron, Whisper FP16·timestamp제거, `.tolist()`·오류 문자열 미세튜닝은 기존결과를 유지한다. 사용자가 제외한 StaticCache/decoder compile·화면지연계측·수동힌트 기본화를 다시 넣지 않는다.
- 기존72회 CER는222cue/1533문자 전체 대비지만 지연 공통집합은59/222다. 네입력 중세개는같은드라마이고모든실행zh지정이었다. 영어·일본어·auto·다른장르의보편성까지입증한점수로확대하지않는다. cue별 globalDP정렬만으로음절의실제시간귀속을확정하지않는다.
- [종합 연구](../../work/asr-deep-review-20261006/ASR_개선가능성_심층검토.md), [현재 코드·로그 감사](../../work/asr-deep-review-20261006/local-audit.md), [모델 조사](../../work/asr-deep-review-20261006/chinese-models.md), [추론·streaming 조사](../../work/asr-deep-review-20261006/streaming-inference.md), [전처리 조사](../../work/asr-deep-review-20261006/frontend-and-models.md), [후속 실험 폴더](../../work/asr-improvements-20261006/).

### 27.1 후속 실제 시험과 적용 판정 (2026-10-06)

- 사용자 "테스트 해보고 적용 할만한것들은 적용해라" 요청으로 격리 시험을 마쳤다. **현재 제품에 적용할 조건을 통과한 후보는 없어 실행 코드·기본값을 유지한다.** 개선 불가능을 증명한 것이 아니라 이번 후보의 품질·지연 조건을 검증한 결과다. E드라이브·최종 배포본·GitHub 변경은 없다.
- 동일 네 입력 약 639.82초, 참고 222cue/1,533문자, 기존 226개 PCM 요청 중 gate 통과 194개를 고정해 새 모델과 auto 품질을 비교했다. 새로운 인명 힌트·정답 주입·유료 API·화면 측정·StaticCache/decoder compile은 없다. 고정 창의 품질 시험과 실제 시간 주입 시험을 혼동하지 않는다.
- **VAD 문맥 보완:** 격리 후보의 CPU 226창에서 기존 194개 통과를 모두 유지하며 sample1-request21만 추가했다. 기존/후보 각각 네 영상, Qwen1.7+HY 동시 적재 실제 시간 재생 8회를 완료했다. 하네스 구버전 호환 실패 2회는 성공 분모에서 제외했다. 제품/시험 소스 보존 검사를 통과했고 실행 오류·미번역 최종 원문은 0이었다.
- VAD 후보의 샘플1 CER은 7.06→6.45%, 삭제 5자 감소/삽입 2자 증가였다. 다른 세 영상의 중국어는 완전히 같았다. 네 입력 합산 9.92→9.72%지만 `些时`가 중복됐고, 후속 `그 시간대에 나타나겠습니다.`가 앞 HY의 완성 번역 뒤에 반복·주체 문제를 만들었다. 대응 지연 p95 변화는 영상별 약 +0.086~+0.153초이며 각 조건 1회·15.625ms 시계로 무악화를 입증하지 못했다. **현재 형태는 미채택**. 전역 dedup 4→2자 완화나 자동 seam 재추론으로 덮지 않는다. 향후 강제 경계의 원문 중복과 이미 표시된 번역의 수정 정책을 함께 검증해야 한다.
- **FireRedASR2-AED:** 고정 공식 모델, BF16/beam3, 194창/오류 0/빈 결과 16. 기존 Qwen→FireRed CER(테스트/샘플1/2/3)은 15.88→14.44 / 7.06→7.66 / 9.32→7.63 / 10.07→12.85%다. `不`·`谁` 누락과 구절·핵심 명사·새 부정 회귀로 미채택했다. 원래 checkpoint FP32·TF32 꺼짐으로 중요 8창을 추가 실행했으며 **8/8이 BF16과 같아 회귀가 유지됐다**. 전체 FP32/beam1/HY 속도 시험은 하지 않았다. 단독 함수 시간을 같은 정확도의 속도 우승으로 쓰지 않는다.
- **Qwen3-ASR-0.6B-hf:** 공식 revision `7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c`, 동일 native/BF16/greedy/zh/빈 prompt, 194창/오류 0. CER 16.25/10.48/11.86/14.24%로 네 입력 모두 현재 1.7B보다 악화했다. `绝无可能`→`就可能` 부정 훼손, `疼`→`走` 등 의미 회귀로 미채택했다. 별도 속도/fullstream 실험으로 넘기지 않았다.
- **기존 1.7B auto 진단:** 194창/오류 0, 177창의 raw text 동일. 감탄사 14창이 빈 전사, 광고 브랜드 하나가 `力TV`→`LiTV`(en)로 변경, 인명 관련 2자 회귀 한 건, 구두점 변경 한 건이었다. CER 16.25/7.46/8.05/7.29%지만 감탄사 생략을 모두 환각 제거로 세지 않는다. 중국어를 다른 언어로 전반적으로 오인식한다는 증거는 없었다. 기존 auto 기본과 사용자의 언어 선택을 유지한다. 이 진단은 auto 실시간 파이프라인 전체 검증이 아니다.
- **DeepFilterNet3 약한 혼합:** 원음 75%+처리 25% 고정 가설이다. 공식 Windows 0.5.6 EXE의 0dB control은 bypass와 30ms crop 때문에 잘못된 제어였음을 확인하고 0.02dB 실제 처리로 교정했다. 수정 control도 impulse 뒤 zero-tail에서 출력 전체가 0이었다. 해당 release는 무음에서 state/STFT/lookahead를 진행하지 않아 flush/timestamp 계약을 보장하지 못함을 공식 소스와 독립 WAV 검사로 확인했다. **원본 영상 전처리·194창 ASR·CER 시험 이전에 중단했다**. 모델 자체의 품질 탈락은 아니다. 기존 Rust/linker가 없어 신규 도구 설치·native 수정은 하지 않았다. 이후 고쳐진 backend의 state/drain 검증 및 30ms 추가 지연까지 확인해야 재검토한다.
- 각 재생의 모델 적재 전 GPU 사용률은 10~43%, 메모리는 3,516~3,925MiB였다. 다른 GPU 활동을 통제하지 않았고 사용자 앱을 임의로 종료하지 않았다. 이번 작은 지연 차이를 후보 코드의 인과 효과로 주장하지 않는다. 품질부터 탈락한 후보를 속도만으로 채택하려는 반복 시험도 하지 않았다.
- .NET 최초 실행 로그의 개발 인증서 문구는 공개 thumbprint/발급 시각을 확인하니 시험 이전 인증서들뿐이었다. 새 인증서 생성이라고 단정하지 않으며 삭제·신뢰 변경·개인키 읽기는 없다.
- 새로 다운로드한 미채택 시험 가중치 2개만 경로·크기·SHA256 검증 후 삭제해 6,296,486,594bytes를 정리했다. 기존 앱 모델은 유지한다. 후보 패치·공식 revision·다운로드 스크립트·설정·해시·원결과·품질 판정은 보존했다. 재시도 전 같은 가중치를 다시 받아야 함을 각 README에 명시했다.
- 최종 검사에서 원래 엔진 소스 68개와 보호 대상 제품·설정·로그·입력 109개의 해시가 일치했다. 추적 소스의 Git 변경이 없고 시험 GPU 프로세스도 종료됐다. [완료 검증](../../work/asr-improvements-20261006/completion-verification.json).
- [최종 시험 결과와 채택 판단](../../work/asr-improvements-20261006/결과.md), [실제 재생8회](../../work/asr-improvements-20261006/replay-analysis/report.md), [VAD 후보 판정](../../work/asr-improvements-20261006/gate/ADOPTION-DECISION.md), [FireRed](../../work/asr-improvements-20261006/firered/QUALITY-DECISION.md), [Qwen0.6](../../work/asr-improvements-20261006/qwen06/QUALITY-DECISION.md), [auto](../../work/asr-improvements-20261006/qwen-auto/DIAGNOSIS.md), [잡음 제거 제약](../../work/asr-improvements-20261006/denoise/DECISION.md), [시험 가중치 정리](../../work/asr-improvements-20261006/test-weight-cleanup.json).

### 27.2 모델 학습 제외 및 근거 표현 정정 (2026-10-06)

- 사용자가 모델 학습을 제외하도록 지시했다. 추가 학습·미세 조정·LoRA·도메인 적응은 이후 개선 후보에서 제외한다. 별도 재개 요청 없이 다시 권하거나 실행하지 않는다.
- 본 프로젝트에서 추가 학습이 현재의 사전 학습 모델보다 정확도를 높이고 속도를 유지한다는 검증 근거는 없다. 모델 크기를 유지할 수 있다는 일반적인 설명을 실제 개선 증거로 취급하지 않는다. 앞서 이를 실용적인 개선안처럼 제시한 표현을 정정했다.
- 공개된 사전 학습 모델을 그대로 사용하는 범위에서 입력 처리·음성 판별·문장 경계·번역 연결·추론 실행을 검토한다. 지금 확인된 구체적인 여지는 판별 단계의 일부 누락 복원이며, 경계 중복과 조각 번역을 함께 해결하기 전에는 적용 가능한 개선이라고 말하지 않는다.
- 현재 결론은 "정확도를 개선·유지하면서 더 느려지지 않는 새 변경을 아직 검증하지 못했다"이다. 막연한 가능성을 구현·시험에 성공한 개선책처럼 표현하지 않는다. 실행 코드·기본값은 변경하지 않았다.

### 27.3 과거 음성 포함 경계 복원과 같은 ID 재번역 시험 (2026-10-06)

- 사용자가 "테스트해보고 실제로 개선효과가 있으면 적용하라"고 요청하여 §27.1 gate-only 후보의 중복·조각 번역을 함께 보완하는 새 후보를 격리 구현했다. **원문 복원에는 성공했지만 한국어 자막 개선을 입증하지 못해 제품에는 미적용**이다. 모델 학습·추가 다운로드·유료 API·화면 계측은 하지 않았다.
- 원래 VAD가 거절한 최종 강제 경계에서만 이미 받은800ms를 붙여 한 번 인식한다. 원래 통과 음성의 파형은 유지한다. 이전4초 창/현재200ms중첩/800ms byte소유권/인접ID/실제 gate분기/zh언어/마지막caption소유권을 검사하고, 한문4자이상 고유 일치 때만 같은ID를 수정한다. 기존 전역dedup를2자로 완화하지 않으며 확장raw를 다음창의 이전원문으로 넘기지 않는다.
- 기존 네 영상226PCM 감사에서 추가 복원 대상은 sample1-request21 하나였다. zh/auto 실제 추론은 모두 `将于今日晚些时段出现，届时。`였으며 앞 원문과 `将于今日晚些时`7자가 일치했다. 최대7.5초·현재마지막ID·번역큐유휴·zh→ko 범위로 연결하고 실패/만료/다음ID/삭제/번역품질실패 시 기존caption을 보존했다.
- 같은 샘플1의180초를 baseline/후보각1회 실제 시간으로 Qwen1.7+HY에 주입해 정상 완료했다. 최종 성공caption의 ID별 마지막버전으로 계산한 원문CER은 **7.0565→6.0484%**,496문자 기준35→30오류(삭제10→5,치환17/삽입8유지)였다. 기존gate-only의2자중복은 없었다. 이 숫자는 사용자참고SRT 대비이며 독립정답·음향경계 검증은 아니다.
- 정식baseline과후보첫번역은 `일곱 별이 연달아 보이는 천문학적 기현상이 오늘 저녁 늦게 나타날 예정입니다.`로 같았다. 후보수정은 `7성 연주의 천문학적 기현상이 오늘 저녁 늦은 시간에 나타날 예정입니다.`였다. 원문의5자가복원됐어도이미전달한핵심정보가늘지않았고가독성이낮아졌으며`届时`도생략됐다. 음악으로확정오역했다고단정하지않는다.
- 첫자막발행53.062초→동일ID수정56.672초로3.610초뒤표현이바뀌었다. 복원PCM끝55.680초→수정0.992초,번역호출61→62회다. 추가음성대기없음은추가계산없음과다르다. 후속정확대응27caption의SRT끝대비p95는1.9039→1.9021초,쌍별최대차이는+0.125초였다.1회pair/부하/확률적MT/15.625ms시계가포함되어속도향상·무악화를주장하지않는다. 화면표시실측이아니다.
- 별도HY진단9회(이전/조각/결합각3회)에서도결합이조각별도표시보다나았지만정식baseline보다일관되게낫지는않았다. 결합3회중1회`7성 연주`,3회모두`届时`생략이었다. 이전3회중1회도음역형이어서후보가항상새오류를만든다고확대하지않는다. 좋은출력만선별하지않는다.
- matcher27+ASR/runtime22+session19=고유CPU68검사통과. 실제반복/부정파형3종×P/C/E=9회추론은정상종료했으며실제gate모두통과라실제복원대상은아니었다. 가정한조건의stress는반복본문보존/거절을확인했을뿐실제반복완전구별을보장하지않는다. UI읽기검토에서엔진7.5초만으로수신/dispatcher후8초만료를보장못하는제약도기록했다.
- 한국어이득이확인되지않아나머지세영상의새후보실시간재생·추가속도반복은진행하지않았다. 네영상기존PCM감사와샘플1새리플레이를구분한다. 같은복원+전체재번역을다시반복하지말고, 새정보를한국어에반영하면서기존올바른의미를보존할수정정책의근거가있을때만재검토한다.
- 제품엔진68개·각리플레이보호대상103개·격리소스해시보존,Git추적변경없음,시험프로세스종료확인. 모델·제품실행코드·기본값·배포본·E드라이브·GitHub변경없음. [상세결과및미채택이유](../../work/asr-boundary-repair-20261006/결과.md), [실재생비교](../../work/asr-boundary-repair-20261006/comparison-r1.md), [최종보존검증](../../work/asr-boundary-repair-20261006/completion-verification.json).

### 27.4 DeepFilterNet3 처리 후 Qwen 실제 인식 비교 (2026-10-06)

- 사용자 "배경음·잡음 처리 후 인식 테스트해봐" 요청으로 §27.1에서 중단했던 전처리를 별도 공식 Python CPU 경로로 시험했다. **약한 처리와 강한 처리 모두 내용 손상·전체 CER 악화가 있어 미채택**이다. 이번에는 실제 원본 영상 전처리와 ASR 품질 비교까지 완료했으므로 이전 EXE state 실패와 구분한다.
- 공식 DeepFilterNet0.5.6/DFN3 revision `978576aa8400552a4ce9730838c635aa30db5e61`, checkpoint SHA `23b92884f63ccf54bb026014604625ab231657b6480df65db4095c4c171e6003`. work 안의 Python3.11.9/Torch2.5.1+cpu/NumPy1.26.4/DeepFilterLib0.5.6에만 설치했다. 기존 앱 runtime·ASR/MT 모델은 수정·복제하지 않았다. CPU4threads, weights_only/strict load, 전체 영상당1회 연속처리, zero tail2초를 사용했다.
- 실제 backend27/helper24검사 통과. libdf의 zero drain·부분hop·작은입력·stateful hop, 모델2frame lookahead·독립session 재현을 확인했다. 공식 pad=True는 STFT480sample을 이미 보정하므로 추가1440sample crop을 금지한다. 원음48→16k 두 변환 경로의 baseline bitwise 일치, 후보길이·해시·clipping을 검증했다. 전체모델 임의chunk streaming 동등성이나 TractEXE와 수치 동등성을 입증한 것은 아니다.
- 기존 네 영상639.82초를 사용해 사전 고정한 원음75%+처리25% 및 처리100%와 원음을 비교했다. 기존 Qwen1.7B/BF16/greedy/zh/힌트없음, 원음gate통과194창×3조건=582회와 full신규gate9창 총591회, 오류0. 창마다 세조건순서순환. 새 원음194 text/language/파형hash는 보존기준과 전부 같았다. 새 학습·클라우드·SRT주입·수동힌트·화면측정·StaticCache/decoder compile은 없다.
- 같은194창 원문CER(테스트/샘플1/2/3): 원음15.8845/7.0565/9.3220/10.0694%, 약한처리15.1625/7.0565/9.3220/12.8472%, 강한처리19.8556/10.0806/11.2288/17.3611%. 전체1,533문자에서 원음152오류9.9152%→약한158오류10.3066%→강한208오류13.5682%. 사용자SRT 기준이며 샘플3개는 같은드라마다. 모든언어·장르의 보편평가로 확대하지 않는다.
- 약한처리 sample3-request30은 `幸运值又涨了`→`新一只幼蟑螂`로 행운수치가 바퀴벌레 관련 문장으로 바뀌었다. 강한처리 sample2-request60 `绝无可能`→`就可能`은 부정 소실, request46 `放开我`→`放裤子`는 명사·동작 훼손이다. sample1-request53의 `谁？`는 직접모델은 인식하지만 처리후VAD가 거절했다. 평균만이 아니라 중요 내용 회귀로 판단했다.
- 실제CPU VAD226창: 원음194통과, 약한192(기존2소실/추가0), 강한197(기존6소실/추가9). 같은원래창에 판별·신규전사를 적용한 별도파생CER는 합계9.9152→10.1761/14.2205%로 역시악화했다. 이는새endpoint/queue/cache를실시간재생한값이아니다. full추가sample1-request21 `接时段主线`은 기대경계문구의정확한복원이아니다. SRT없는감탄사·추가문구를음향확인없이전부환각으로세지않는다.
- 실시간적용시알고리즘상약30ms추가대기가있다. 전체파일처리2.98~5.48초는배치비용이며실시간자막지연으로쓰지않는다. 정확도가먼저탈락하여HY동시번역·native streaming구현·실시간속도시험으로확대하지않았다. Whisper/영어/일본어/음원분리전용모델은이번범위밖이다. 모든배경음처리가불가능하다는결론이아니며, 같은DFN설정과네영상에서혼합비율만조정하는반복을피한다.
- 최종stdlib검사에서제품엔진·설정·로그101파일,기존모델,원래로그,동결소스,transformers모듈,패키지버전,원음manifest보존을확인했다. Git추적소스변경없음. 이번개발기록만추가하며제품실행코드·기본값·E드라이브·배포본·GitHub변경없음. [결과와채택판정](../../work/asr-denoise-test-20261006/결과.md), [약한처리평가](../../work/asr-denoise-test-20261006/weakmix25/evaluation.md), [강한처리평가](../../work/asr-denoise-test-20261006/full100/evaluation.md), [최종검증](../../work/asr-denoise-test-20261006/completion-verification.json).

### 27.5 동일 Qwen1.7B의 ORT CUDA 디코더 교체 시험 (2026-10-06)

- 사용자 "같은 Qwen 모델의 추론 엔진 교체 테스트" 요청으로 기존 BF16 Qwen3-ASR-1.7B-hf를 유지한 격리 시험을 완료했다. **ASR 계산은 약 22% 빨라졌지만 실제 HY 자막 발행 단축은 중앙값 약 63ms여서 제품 기본 엔진에는 미적용**이다. 변환 파일 3.44GB, 준비시간 약 4.7초 증가와 함께 판단했다. 가속 자체가 불가능하다는 결론은 아니다.
- 전체 ASR를 ONNX로 옮긴 것이 아니다. 기존 native processor·음성 encoder·projector·embedding·파서·CUDA RoPE를 유지하고 디코더 28층만 ORT1.26 CUDA로 실행했다. 원래 310개 디코더 tensor는 역전치 후 BF16 비트가 모두 같았다. 기존 safetensors SHA는 `2db53c7d81bd9b8cbc6a074e89be2c968a0d373fb4ee68bb1b1e14f7042dfee1`이다. 동적 KV를 GPU에 보존하며 학습·양자화·StaticCache·torch.compile·CUDA graph·TensorRT·힌트·클라우드·화면 측정은 하지 않았다.
- work의 별도 ORT GPU 패키지와 기존 Torch CUDA12.8 DLL을 사용했다. 앱 runtime을 수정하지 않았다. 첫 실행의 CPU fallback 전면 금지는 shape helper 배치에서 초기화 실패했으며 추론 실패/품질 분모에 넣지 않았다. CPU shape helper 허용 후 실제 GPU profile을 검사했다. 최초 그래프의 CPU Sin/Cos를 native CUDA RoPE로 대체해 최종 CPU 연산은 shape Gather/Add만 남겼고 주요 MatMul/Softmax는 CUDA였다. 변형 전후 후보 원문·토큰 194개는 모두 같았다.
- 네 영상 총 639.82초, 기존 gate 통과 194창, 중국어 지정·빈 힌트·greedy·최대256token·EOS 조건으로 비교했다. 원문 CER은 양쪽 모두 테스트15.8845/샘플1 7.0565/샘플2 9.3220/샘플3 10.0694%, 합계152/1533=9.9152%였고 S/D/I도 같았다. 원문 raw190/194, 정규화193/194, token189/194가 같다. 구두점3창·`啊→嗯`1창·같은 문자열의 token분할1창이 달랐다. 감탄사 창은 대응 SRT가 없어 음향 정답을 판정하지 않았다. CER 동등을 완전한 의미·token 동등으로 확대하지 않는다.
- 순서를 균형 배치한194창×2엔진×3회=1,164호출은 오류0, 각 backend 자체 결과는3회 모두 재현됐다. 계산 p50은303.278→235.525ms, p95는639.832→500.643ms, 합계196.850→151.753초(22.909% 감소)였다. 같은 입력·반복의 후보−기존 차이는 p50−67.519ms/p95−33.652ms/max+12.289ms이고581/582쌍이 빠르다. 입력별3회 중앙값은194개 모두 빠르다. 이 범위는 정규화된 파형→processor/encoder/decoder/parser와CUDA완료이며 음성 수집/VAD/MT/발행은 제외한다.
- ORT 버전 변경에 따른 VAD 회귀를 따로 확인했다. 앱1.30 CPU EP와 격리1.26 CPU EP는226창 및32,392개의 연속20ms VAD패킷에서 확률·판정·RNN상태가 바이트 동일했다. 실제 라이브 리플레이 외부 주입은100ms/3200bytes이고 내부VAD프레임이20ms다. wrapper의20ms실주입 설명은 잘못된 metadata 문구로 원시 기록을 보존하고 보고서에서 정정했다.
- 샘플1 180초를 새 baseline/후보 프로세스 각1회에서 HY-MT2-7B-Q6_K와 함께 실제 시간 주입했다. 원문61개 전체내용·순서 동일, CER7.0565%, ASR66/MT61, 실패·미번역·수정·삭제0, 양쪽tail정상이다. 고유 원문59쌍의 자막 발행 차이는 p50−63ms/p95−15.3ms/max+62ms였고 같은 한국어47쌍도p50−63ms다. 첫 자막8.610→8.593초, 마지막181.125→181.015초였다. 숫자ID로 cross-run 대응하지 않고 실제 원문 배열 전수 재구성 후에만 음성창 소유권을 연결했다.
- 한국어48/61개 동일·13개변동이며 실제HY입력 원문·언어·앞원문context는61쌍 모두 같았다. 일부는 설명력·정확도가 낮아지고 일부는 자연스러워졌다. 이 변동을 ASR 원문 회귀라고 단정하거나 한국어까지 완전 동등하다고 주장하지 않는다. 별도 한국어 정답 점수는 없다. 실제 시계GetTickCount64/15.625ms·조건별1회·외부GPU활동·평균약9ms주입지각을 포함하므로 작은 시간차를 보편적 인과효과나 무악화보장으로 확대하지 않는다. 화면 측정은 없다.
- 준비시간15.235→19.891초. 준비완료GPU전체snapshot13,101→13,254MiB이며 최대/프로세스전용 메모리가 아니다. 후보초기화에서native만5.07GiB→양decoder9.21GiB→원래Torch28층해제후6.60GiB로 별도관측됐다. 서로 다른 시점의 메모리 차이를 혼합하지 않는다. RTX4080 16GB에서OOM없이완료했지만작은GPU는미검증이다. 변환파일3,441,149,952bytes와원래safetensors가현재혼합구현에둘다필요하다. 두ONNX그래프는data한개를공유한다.
- llama.cpp는 최신 공식 converter·audio frontend·현재HF헤더까지 조사했다.707개이름매핑은가능하지만native유효길이/padding attention/언어prompt가달라동일조건단순교체가아니다. 실제대형GGUF변환·추론은하지않았으며"품질시험실패"로기록하지않는다. ORT의test-only어댑터도제품폴더로단순복사하지않는다.
- 제품엔진·설정·로그101파일,동결소스68개,기존모델/Transformers/패키지/원음/원래로그보존검사통과,Git추적소스변경없음,시험GPU프로세스종료확인. 이번개발기록외제품실행코드·기본값·E드라이브·릴리즈·GitHub변경없음. 같은그래프/입력의반복시험은피하고배포비용감소나새엔진구체근거가있을때만후속검토한다. [결과 및 판단](../../work/asr-backend-test-20261006/결과.md), [R3 계산시간](../../work/asr-backend-test-20261006/timing-native-rope-r3/ANALYSIS.md), [실시간 번역 비교](../../work/asr-backend-test-20261006/replay-analysis/summary.md), [보존 검증](../../work/asr-backend-test-20261006/completion-verification.json).

### 27.6 정확도 우선·지연 비악화 조건 재확인 (2026-10-06)

- 사용자가 "1.정확도 우선(현재 보다 느리면 안됨) 2.속도. 현재 상태에서는 속도가 우선인것이 아니다"라고 우선순위를 명시했다. 앞선 답변이 남은 후보를 속도 중심으로 추천한 것은 우선순위 해석 오류였으며 이를 정정한다.
- 1순위는 현재 지연을 늘리지 않으면서 원문 인식과 최종 자막의 정확도를 개선하는 것이다. 더 빨라지지 않아도 정확도가 개선되고 속도가 유지되면 가치가 있다. 정확도가 좋아져도 현재보다 느려지는 변경은 요구 조건을 통과하지 못한다.
- 2순위는 정확도를 유지하면서 속도를 개선하는 것이다. 경량 모델·스트리밍 모델·추론 엔진 교체는 빠르다는 이유만으로 정확도 개선 후보보다 우선하지 않는다. 앞서 언급한 Moonshine·Fun-ASR-Nano는 실제 품질 우위가 확인되지 않은 후보이며, 속도 가능성만으로 우선 추천하지 않는다.
- 후보 판정은 평균 CER와 함께 누락·중복·부정·숫자·인명·번역 의미 손상을 검사한 뒤, 같은 입력·번역 조건에서 로그로 전체 자막 지연 비악화를 검증한다. 단일 실행의 작은 차이나 ASR 계산시간만으로 속도 유지가 입증됐다고 하지 않는다. 학습·수동 힌트·화면 측정 등 기존 제외 조건은 유지한다.
- 이번 정정은 향후 평가·추천 기준의 변경이다. 기존 시험 결과를 다시 쓰거나 미채택 후보의 효과가 새로 확인된 것처럼 해석하지 않는다. 새 모델 다운로드·추론 시험·제품 실행 코드·기본값·배포본·E드라이브·GitHub 변경은 하지 않았다.

### 27.7 정확도 중심 재조사와 실제 오류 연결 감사 (2026-10-06)

- 사용자 "정확도를 기준으로 다시 자세하게 조사하라" 요청에 따라 중국어·영어권 공식 모델 카드·논문·공개 코드·개발자 issue와 기존 시험을 재검토했다. **정확도 개선 + 한국어 의미 비악화 + 현재 지연 유지**를 채택 조건으로 삼는다. 원문이 좋아졌을 때 한국어에 반드시 새 정보가 추가돼야 한다는 조건은 붙이지 않는다. 모델 크기·속도·streaming 지원만으로 후보 순위를 높이지 않는다.
- 네 영상의226개 ASR 요청과209개 HY 입력을 실제 PCM 소유권→raw→후처리→번역 순서로 전수 재구성했다. 현행 overlap 제거가 문자를 잘라낸 사례는0건이다. `祈使句→歧视句`, `真没礼貌→真没用吗`, `工资赔→工资呗`, `循环→雪花`, 짧은`不` 누락은 raw 단계부터 참고자막과 어긋난다. `茶里茶气→查理查/茶器` 등 강제 경계 사례도 연결했으나 음소가 실제 어디서 잘렸는지는 새 청취 없이 단정하지 않는다.
- 기준 CER152/1533=9.9152%(53치환/25삭제/74삽입)는 기존 정책을 그대로 재현했다. SRT 밖 감탄·광고·동음 인명 표기를 포함하므로 삽입74개를 환각74개로 바꾸어 말하지 않는다. 세 sample은 같은 드라마이고 모두zh 지정이므로 영어·일본어·auto의 일반 성능 증거가 아니다. 20개 대표 사례에 raw/최종/한국어/PCM 범위/SRT를 보존했다.
- HY의 동일 원문·언어·앞 문맥 209개 중 73개는 기존 3회에서 한국어 표현이 달랐다. 오류 수가 73이라는 뜻은 아니다. `关我的事儿→내 알 바 아니야`처럼 MT의 부정 추가도 ASR 손상과 분리했다. 현재 HY sampling은 공식 권장값과 맞는다. **sampling 대 단일 greedy의 의미 보존 비교**는 과거 D1 sampler 순서/CR1 문맥 길이 시험과 다른 미시험 가설이다. 결정성이 정확도를 보장하지 않으며 출력 길이와 지연 검증도 필요하다.
- 단일 ASR의 **짧은 자동 이전 원문 문맥**은 현재 Qwen이 사용하지 않는 미시험 방향이다. 수동 힌트·정답 SRT·번역을 넣지 않고 해당 candidate 자신의 직전 출력만 참조해야 한다. 공식 #186에는 context가 출력에 섞이거나 약한 음성을 대체하는 반례가 있다. 상시 적용하지 않고, 틀린 이름/새 주제/부정/화자 전환에서 오류 전파와 prefill 비용을 격리 검증한다. 기존 800ms 음성 확장+재번역과 구분한다.
- moona3k의 speech-aware 경계 코드를 고정 SHA로 읽고 기존 maximum_window 82창을 CPU로 감사했다. 300ms energy는 55창을 10–290ms 이동하고, 120ms pause를 요구한 300ms 변형은 7창을 105–235ms 이동했다. 대표 `茶里茶气` 창은 각각 10ms/0 이동이어서 그 오류를 해결했다고 말할 수 없다. 원 upstream 2초 commit은 추가 decode 1회를 쓰고 최대 1.905초 carry가 생겼다. 최초 ASR 전 경계 선택 변형도 carry 발행 지연·호출 수·Silero state·speculative cache 소유권 검증이 필요하다. 이는 원래 창 선택 감사이며 새 controller 전사 시험이 아니다.
- MOSS0.9B는 Movies CER6.36과 공개 BF16 약1.82GB가 조건부 품질 선별 근거다. Qwen 직접 비교가 없고 30초 feature padding/태그 생성 비용이 있다. GLM은 quiet speech 특화 근거가 있지만 총 BF16 약4.52GB/30초 padding이며 Qwen 측 다국어 평가에선 뒤처진다. 둘 다 현재 4초 입력·HY 공동 실행에서 정확도/지연 우위를 입증하지 않았다. Dolphin은 방언 직접 비교가 유리하지만 CV-TW 5.62 대 Qwen3.92로 불리하고 offline 표와 공개 streaming 가중치 대응을 구분해야 한다.
- FireRedASR2-LLM은 공식 중국어 평균 CER2.89 대 Qwen3.76으로 직접 근거가 있다. 이미 시험해 실패한 AED와 별도 미시험 모델이다. 다만 decoder 약15.23GB에 encoder/HY가 추가되어 16GB 공동 상주에 부적합하다. MiMo-V2.5-ASR도 일부 가사/혼합 언어 개선이 있지만 F32 배포 약32.07GB·BF16 본체 약16GB에 audio tokenizer/HY가 추가된다. 양자화/offload/교대 적재가 정확도와 지연을 보존한다고 가정하지 않는다. Hojo/VibeVoice streaming/BitNet/X-ASR/Fun/SenseVoice 등도 새 모델이라는 이유로 우선하지 않고 불리한 비교·언어 범위·정밀도를 기록했다.
- Whisper-CD는 학습 없지만 greedy보다 느리고 Qwen 전사는 향후 연구다. TAD는 환경음 Yes/No QA라 전사 근거가 아니다. confidence calibration은 greedy 정답을 바꾸지 않으며, 학습이 필요한 SAE/Calm류는 제외했다. 현재 native 4초/batch1에는 공식 #213의 옛 backend 8초 mask 문제/#207의 가변 batch 문제를 그대로 적용할 근거가 없다. GTCRN/ClearerVoice의 청감 개선을 Qwen CER 개선으로 대신하지 않는다. 같은 DFN·Qwen0.6·FireRedAED·Nemotron·3초 절단·재확인 설정은 새 근거 없이 다시 시험하지 않는다.
- ORT의 대응 ASR 중앙 약67ms 절감은 정확도 추가 기능의 비용을 상쇄할 가능성일 뿐 보장된 여유가 아니다. 현행 native/ORT만/ORT+품질 후보를 분리하고 준비시간까지 검토한다. 정확도 후보 통과 후 동일 HY와 100ms 실시간 주입, 순서 교차 반복, first/p50/p95/tail/queue/새 복구 문장 지연을 로그로 확인한다. 평균만 좋거나 일관된 느려짐/불확실성이 남으면 적용하지 않는다. 현 네 영상은 개발에 사용했으므로 새 holdout으로 부르지 않는다.
- 이번 작업은 새 GPU 시험 없이 기존 로그 재구성·공식 작은 소스/metadata 확인·CPU 경계 감사만 했다. 제품 실행 코드·기본값·모델·개인 설정·E드라이브·배포본·GitHub 변경 없음. 학습·수동 힌트·화면 측정·StaticCache/decoder compile 제외를 유지한다. [종합 판단과 후속 순서](../../work/accuracy-first-review-20261006/정확도우선_재조사.md), [20개 사례](../../work/accuracy-first-review-20261006/error-evidence-cases.md), [로컬 감사](../../work/accuracy-first-review-20261006/local-error-audit.md), [모델 조사](../../work/accuracy-first-review-20261006/models-accuracy.md), [알고리즘 조사](../../work/accuracy-first-review-20261006/algorithms-accuracy.md), [입력·번역·평가 조사](../../work/accuracy-first-review-20261006/frontend-and-evaluation.md).

### 27.8 모델 유지: 자동 문맥·경계 조정 실제 시험 (2026-10-06)

- 사용자가 모델 교체를 제외하고 1번 자동 문맥·3번 경계 조정을 시험하도록 요청했다. 프로그램은 영화·드라마 전용이 아니므로 작품·인물·장르별 규칙을 넣지 않았다. 현재 Qwen1.7B/BF16/native/greedy와 HY 모델을 유지했다. **두 고정 후보 모두 품질 조건에 실패해 제품에 적용하지 않는다.** 추가 모델·학습·수동 힌트·SRT prompt·ORT·화면 측정은 없다.
- A(context32)는 직전 자연 종료한 확정 원문의 마지막 완성 문장 하나를 최대32 tokenizer token으로 같은 ASR에 제공했다. 강제4초/stream-end/미완성/겹친 PCM/빈 문구/PCM 간격3초 초과는 제외했다. 후보 자신의 과거 결과만 사용하고 window별 history를 동결했다. 기존 snapshot/accept의 인과 순서를 유지한194 gate-pass 고정창 baseline/후보 교차388호출이며, 새 baseline194개 원문·언어·PCM hash는 보존 기준과 동일했다. 실제 prompt119개, raw 변경16개, 오류0, empty/cache 일정 불일치0이다. 이 단계는 실제 자막 지연 시험이 아니다.
- A CER(테스트/샘플1/2/3)는15.8845/7.0565/9.3220/10.0694%에서14.8014/7.6613/12.5000/12.5000%가 됐다. 전체152/1533=9.9152%→174/1533=11.3503%. `雪花→循环`, `工资呗→工资赔` 복원은 인정한다. 그러나 sample1-r19 `嗯→散会`, sample2-r31 `嗯→你每次见到我，就只有这一句话说吗`, sample3-r48 `啊→又生气了，走为上策`처럼 앞 문맥 전체가 재출력됐다. 세 경우 모두 폐기된 잠정 결과가 아니라 최종 원문이다. 평균·의미 회귀 때문에 미채택하며 HY/실시간 단계로 확대하지 않았다.
- A helper의 special-token guard가 tokenizer의 전체 marker를 처음부터 포함하지 않은 한계가 독립 검토에서 발견됐다. 모든 실제 prompt119개와 baseline/candidate194개·보존 원문226개에 전체 marker/angle marker 재검사 결과0건이므로 이번 수치·재출력 원인은 아니다. 실행 도중 helper를 변경하지 않고 실행 SHA를 보존했다. 향후 재사용에는 보완이 필요하지만 실패한 후보를 제품에 넣지 않았다.
- B(pause300)는 최초 ASR 전 최대4초 경계에서 마지막300ms의20ms frame을 검사해, 전체 창 median RMS의0.5 이하가120ms 지속된 구간으로 경계를 이동했다. 이미 speculative 요청이 있거나 실제 양의 RMS carry가 없으면 원래 경계를 유지했다. 이동 PCM·기존200ms overlap·VAD 단일 진행·짧은 carry 발화 소유권을 보존했다. 전용16개+기존62개 CPU 검사 통과, 네 원음×가상 추론 지연0/0.3/0.8초의12조건에서 PCM/cache 검사 통과. fake 문자열은 품질 평가에 쓰지 않았다.
- 실제 B 품질은100ms PCM 주입→실제 native 전사→실제 accept의 순차 controller로 시험했다. ASR 동안 가상 오디오는 진행하지 않아 실제 지연을 측정한 것은 아니다. baseline/후보의 자체 새 창과 final을 각각 재구성했다. baseline 원문209문장은 기존 async R1과 완전히 같았다. baseline226/candidate227 전사 경로 호출이며 VAD 거절도 포함된 수치다. 각각 cache2회, final220→221, 문장209→208이다. 모든 snapshot PCM slice/hash와 cache/final 소유권을 검사했다.
- B 실제 경계 이동은 테스트0/샘플1 3/샘플2 5/샘플3 2, 총10개이며60–240ms였다. CPU 기록과 전 필드가 일치했다. 상대적 저에너지 구간일 뿐 실제 무음을 검출했다고 부르지 않는다. 10개 중6개는 저에너지가4초 끝까지 이어졌고 모든 carry에는 절대 RMS 양의 frame이 있었다. 최초 인식 전 경계 이동이 추가 오디오 대기를 없애더라도 carry 발행 지연과 호출 증가까지 없애는 것은 아니다.
- B CER는15.8845/7.0565/9.1102/10.0694%, 합계151/1533=9.8500%다. 전체1문자 감소지만 `现在是我的私人时间。→现在是我的私人。/私人事情。`로 시간 의미가 사적 사정으로 훼손됐고, `希望不是果酱。→希望不是国家。`로 잼이 국가가 됐다. SRT 밖 감탄/꼬리 문구 감소와 중복 감소가 새 명사 오류를 평균에서 상쇄한 것이다. 정확도 우선 조건에 따라 미채택하고 실시간 HY 지연 시험으로 확대하지 않았다.
- A/B를 합치거나 결과를 보고 prompt 길이·pause 기준을 같은 자료에 맞춰 다시 조정하지 않았다. 이번 결과는 모든 문맥/분절 방법의 불가능성 증명이 아니라 시험한 두 고정 정책의 탈락이다. 기존 네 중국어 입력 중 세 개는 같은 드라마이며, 확인 범위에서 추가 타언어 PCM+정답 쌍은 없었다. 영어·일본어·모든 장르의 검증을 수행했다고 주장하지 않는다. 모델 교체 후보는 현재 사용자 범위에서 제외한다.
- 완료 검사에서 제품 엔진·설정·로그101파일, 격리 baseline110파일, 모델·원음226창·원래 로그·Transformers·runtime package·후보 소스 보존을 확인했다. Git 추적 변경 없음, 시험 GPU 프로세스 종료. 제품 실행 코드·기본값·개인 설정·E드라이브·배포본·GitHub 변경 없음. [결과와 사례](../../work/context-boundary-test-20261006/결과.md), [A 평가](../../work/context-boundary-test-20261006/evaluation/context-r1.json), [B 자체 창 평가](../../work/context-boundary-test-20261006/evaluation/controller-boundary-r1.json), [B 구현/검증](../../work/context-boundary-test-20261006/boundary/README.md), [독립 검토](../../work/context-boundary-test-20261006/review/PRETEST-REVIEW.md), [보존 검사](../../work/context-boundary-test-20261006/completion-verification.json).
