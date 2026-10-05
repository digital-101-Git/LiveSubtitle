# Third-party runtime notices

These files preserve the original licenses and notices for named components in
the LiveSubtitle v1.0.0 Windows runtime. LiveSubtitle's own source is MIT licensed;
this does not relicense third-party code, NVIDIA/Intel binaries, or separately
downloaded models. Files marked as upstream originals in [manifest.json](manifest.json)
are byte-for-byte copies. The manifest records their source URLs and SHA-256 hashes.
Original notices elsewhere in `runtime/` must also be retained.

| Component | Included original notice |
|---|---|
| llama.cpp b11378 | [MIT](LICENSE-llama.cpp-b11378.txt) |
| CTranslate2 4.8.2 | [MIT](LICENSE-CTranslate2-4.8.2.txt) |
| faster-whisper 1.2.1, locally maintained PCM fork | [MIT](LICENSE-faster-whisper-1.2.1.txt); changes and build provenance in `engine/dependencies/faster_whisper_pcm/` |
| Microsoft.NETCore.App.Runtime.win-x64 10.0.12 | [MIT](DOTNET-NETCore-Runtime-10.0.12-LICENSE.txt), [third-party notices](DOTNET-NETCore-Runtime-10.0.12-THIRD-PARTY-NOTICES.txt) |
| Microsoft.WindowsDesktop.App.Runtime.win-x64 10.0.12 | [MIT](DOTNET-WindowsDesktop-Runtime-10.0.12-LICENSE.txt) |
| Tokenizers 0.23.2 | [Apache-2.0](LICENSE-tokenizers-0.23.2.txt) |
| FlatBuffers 25.12.19 | [Apache-2.0](LICENSE-flatbuffers-25.12.19.txt) |
| Intel OpenMP 2025.3.2.833 / 2025.3.0.640 (NuGet) | [ISSL](IntelOpenMP/LICENSE.txt), [OpenMP third-party notices](IntelOpenMP/third-party-programs.txt), [scope](IntelOpenMP-SCOPE.txt) |
| Embedded oneTBB allocator (2022.2.0) | [package license](IntelOpenMP/oneTBB/LICENSE.txt), [complete oneTBB third-party notices](IntelOpenMP/oneTBB/third-party-programs.txt) |
| CUDA 12.4.1 with llama.cpp | [versioned EULA](CUDA-12.4.1-EULA.html) |
| CUDA 12.8.1 with PyTorch | [versioned EULA](CUDA-12.8.1-EULA.html), [CUPTI 12.8.90 package notice](CUPTI-12.8.90-LICENSE.txt) |
| CUDA Runtime 12.9.79, NVRTC 12.9.86, cuBLAS 12.9.2.10 | [Runtime](NVIDIA-CUDA-Runtime-12.9.79-License.txt), [NVRTC](NVIDIA-CUDA-NVRTC-12.9.86-License.txt), [cuBLAS](NVIDIA-CUBLAS-12.9.2.10-License.txt) |
| cuDNN 9.19 / 9.27 | [9.19 EULA](cuDNN-v9.19.0-EULA.html), [9.27 EULA](cuDNN-v9.27.0-EULA.html), [9.27.0.42 wheel notice](NVIDIA-CUDNN-9.27.0.42-wheel-License.txt) |

## Scope and component terms

The .NET licenses above come from the actual runtime packs, not the SDK-root
Microsoft .NET Library license. The development SDK is not included in the release.

The PyTorch Intel binaries are obtained from the official
[intelopenmp.redist.win 2025.3.2.833](https://www.nuget.org/packages/intelopenmp.redist.win/2025.3.2.833)
NuGet package and remain unmodified. They are byte-identical to the two DLLs
previously present in the development environment. CTranslate2's separate
`libiomp5md.dll` is obtained unchanged from
[intelopenmp.redist.win 2025.3.0.640](https://www.nuget.org/packages/intelopenmp.redist.win/2025.3.0.640),
also byte-identical to its existing DLL. These two packages supply the same
Intel Simplified Software License (October 2022) and OpenMP notices. Preserve the
license and copyright notices, including the complete OpenMP and referenced
oneTBB notices. Intel's component restrictions do not change the licenses of
unrelated MIT or Apache-2.0 code. No separate TBB/TCM binaries are added here.

NVIDIA CUDA, cuBLAS, NVRTC, CUPTI and cuDNN remain subject to their respective
NVIDIA terms. Use the included copies with LiveSubtitle and comply with the
applicable restrictions on redistribution, modification, reverse engineering,
notices and use; the app's MIT license grants no additional rights over these
NVIDIA components. Read the original agreements for their full scope and any
exceptions. Independent open-source components retain their own licenses.
The CUPTI notice covers the retained CUDA component; it is not a claim of
redistribution permission for `nvperf_host.dll`.

The Windows PCM release omits PyAV and `av.libs` (including FFmpeg/x264/x265),
SoundFile and its bundled libsndfile, and `torch/lib/nvperf_host.dll`. Local
development installations may still contain those optional files. Their presence
in a development folder does not include them in the release or license them
under these documents. If a custom distribution restores them, review the exact
versions and applicable source/distribution requirements before redistributing.

`cuDNN-EULA-20261005.html` is a retained snapshot of NVIDIA's latest-documentation
page on the review date. Use the matching **versioned** cuDNN documents above
for the identified components. Do not replace their scope with a newer latest page.

Additional original notices remain with CPython (`runtime/python/LICENSE.txt`),
PyTorch (`*.dist-info/LICENSE` and `NOTICE`), other Python packages
(`*.dist-info/licenses/`), ONNX Runtime (`LICENSE` and `ThirdPartyNotices.txt`),
LLVM OpenMP (`runtime/llama/LICENSE-LLVM-OpenMP`), and vendored source under
`engine/_vendor/`. Model weights are downloaded separately and retain their
model-specific licenses and terms.
