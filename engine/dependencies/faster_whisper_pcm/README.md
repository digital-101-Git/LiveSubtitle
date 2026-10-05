# faster-whisper PCM dependency

LiveSubtitle uses **faster-whisper 1.2.1+livesubtitle.pcm1**, built from the pinned
upstream wheel by `../build_pcm_whisper.py`. Browser and microphone capture pass
16 kHz mono NumPy PCM arrays into ASR. They do not need a media-file decoder.

The maintained `audio.py` delays importing PyAV until file decoding is requested.
`audio.patch` records the small source change against
[upstream v1.2.1](https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1).
The unchanged upstream MIT license is in `LICENSE` (Copyright 2023 SYSTRAN).
The local change was made for LiveSubtitle by digital-101 in 2026 under MIT.

The builder changes only `audio.py`, the local version, and distribution metadata.
It moves `av>=11` from required dependencies to the optional `audio` extra,
rebuilds `RECORD`, and includes `livesubtitle-pcm.json` provenance. All other
upstream code/assets remain byte-identical, including the Silero-v6 VAD used by
the app. The verified working app's `vad.py` and `silero_vad_v6.onnx` were already
identical to this official wheel; no extra VAD replacement is needed.

## Source installation

From the app root, using the intended Python 3.12 environment:

```powershell
python engine/dependencies/build_pcm_whisper.py --install
```

Or build then use the ordinary requirements command:

```powershell
python engine/dependencies/build_pcm_whisper.py
python -m pip install -r engine/requirements.txt
```

The helper downloads only the approximately 1 MB pinned upstream wheel, checks
its SHA256, and builds the local wheel in `.dependency-build/`. It does not
download ASR or translation model weights. `--upstream-wheel PATH` allows an
offline build from the exact wheel. Fixed ZIP timestamps and stored entries make
the generated artifact deterministic; its hash is written beside the wheel.
Do not install upstream `faster-whisper` afterward: it would restore mandatory
PyAV and erase the local dependency changes.

SoundFile is not a base requirement either. Existing environments are not cleaned
by this command: pip does not uninstall previously installed PyAV or SoundFile.
Use a clean environment for a source build or the audited release staging process.
Arbitrary media-file input to `faster_whisper.decode_audio()` requires optional
PyAV; this app's live PCM input continues to work without it. Adding the optional
`audio` extra requires its own codec licensing and redistribution review.

## Release staging

The release builder can update an independent staging runtime without importing
the models or reinstalling dependencies:

```powershell
python engine/dependencies/build_pcm_whisper.py --upstream-wheel PATH --apply-site-packages STAGING/runtime/python/Lib/site-packages
```

This validates every upstream package file and refuses changed VAD/transcription
code, linked package directories, or this app's working runtime. It updates only
the two changed Python files and versioned metadata. Codecs must be excluded by
the release packager separately; this helper never removes those other packages.

## Pinned provenance

| Artifact | SHA256 |
| --- | --- |
| [Official 1.2.1 wheel](https://files.pythonhosted.org/packages/05/99/49ee85903dee060d9f08297b4a342e5e0bcfca2f027a07b4ee0a38ab13f9/faster_whisper-1.2.1-py3-none-any.whl) | `79a66ad50688c0b794dd501dc340a736992a6342f7f95e5811be60b5224a26a7` |
| Upstream `audio.py` | `60a1d8638f718cbf6d245aed3e5a5aa61c1f822a0b0fe9b48a7c928d47c23909` |
| PCM `audio.py` | `e47cb57c67cdb61a4910db03fffec965223d41ca48982d7238db892c1a470016` |
| Unchanged `vad.py` | `37a9c774aefdd3162d936b896c8dcf5571b2ed938d65bffecfd631770049a18d` |
| Unchanged `silero_vad_v6.onnx` | `4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2` |

The VAD asset is obtained from the verified upstream wheel during setup. It is
not duplicated into this source folder. Upstream Silero copyright/permission
notices are retained by the application's third-party license bundle.
