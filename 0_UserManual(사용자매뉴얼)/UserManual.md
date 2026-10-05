# LiveSubtitle User Manual

[한국어](사용자매뉴얼.md) · English · [中文](用户手册.md) · [日本語](ユーザーマニュアル.md)

## First-time setup: if you only have the app

**If you do not have any model files, download them first and put them in the designated folders.** The paths below are relative to the app folder containing `LiveSubtitle.exe`.

These instructions assume that the runnable app and its runtime are already available. If you downloaded only the source from GitHub, first prepare the app and runtime by following the [development environment and build instructions](../SOURCE_RELEASE.txt).

1. Download **one local speech recognition model and one translation model** from the **Verified model downloads** table below.
2. Place the downloaded files in the following locations. If a folder does not exist, create it with the same name.

| Downloaded model | Destination |
|---|---|
| Qwen3-ASR-1.7B | `models/asr/qwen3-asr-1.7b/` |
| Whisper large-v3-turbo | `models/asr/whisper-large-v3-turbo/` |
| Translation model `.gguf` file | `models/translation/` |

The supplied Qwen and Whisper folders already contain configuration and tokenizer files. Put Qwen's `model.safetensors` or Whisper's `model.bin` directly inside the corresponding folder above. For translation models, place the `.gguf` file in its folder **without changing the original filename**.

3. Run **LiveSubtitle.exe**, select both models, and click **Prepare models**. Once preparation is complete, follow the steps below to start captions for your stream.

## Running the app

1. Run **LiveSubtitle.exe** in the app folder. You do not need to start Ollama or Python separately.
2. Under **Audio source**, choose the Chrome or Edge window playing audio, **All system audio**, or **Microphone · device name**. If you have just connected a microphone, click the refresh button next to the list.
3. Select **Local recognition**, then choose a speech recognition model and a translation model.
4. Set **Input language** to Korean, English, Chinese, or Japanese. With **Automatic detection**, the model determines the language.
5. Under **Translation language** directly below it, choose Korean, English, Chinese, or Japanese. The default is **Korean**, and your selection is saved for the next launch.
6. Once **Prepare models** has finished, click **Start live captions**.

- Use **Language** at the upper right to switch the interface to **한국어 · English · 中文 · 日本語**. Menus, instructions, and the names in the input and translation language lists update immediately, and the setting is saved for the next launch. You can change it while captions are running; the selected input and translation languages and the actual caption text remain unchanged.
- Click **Stop** before changing the audio source, models, input language, or translation language. If the input and translation languages are the same, captions show the recognized original text.
- Under **Speech confirmation mode**, **Default** uses the default recognition and display method. **Repeated confirmation (slow)** compares multiple recognition results before displaying text, so it may increase delay and can still leave recognition errors.
- Microphone mode uses audio from the selected input device. If there is no input, check the device connection and Windows microphone access permissions. Microphone captions appear on the monitor by default, and you can edit their position and size.
- The app must keep running while captions are displayed. Closing its window leaves it running in the system tray. Click **Quit** to exit completely.
- The translations and original text in **Recent captions** are read-only. Select text with the mouse and press **Ctrl+C** to copy it.
- Clicking **Clear history** at the top deletes translation history and log files in the `logs` folder and clears the recent captions shown in the app. Live captions continue processing, and new records are saved afterward. Files that could not be deleted are listed in the activity log.
- To use Gemini transcription, select **Gemini Live** and enter your API key. Audio from the selected source is sent to Google, while the selected local model handles translation.

## Speech recognition hints

Hints tell the speech recognition model **how character names are written in the original language**. They may help reduce errors where similar pronunciation causes a name to be transcribed with the wrong characters. Hints **apply only to local Qwen3-ASR**; the translation model decides how names are rendered in the selected translation language.

1. If captions are running, click **Stop**.
2. Under **Recognition hints · Optional**, enter names **in their original language, one per line**.
3. Click **Save recognition options**, then **Start live captions**.

**When a person's name appears in the original text in Recent captions**, select it with the mouse, press **Ctrl+C**, and paste it into the hints field with **Ctrl+V**. Put each name on a separate line. Correct any misrecognized names to their proper original spelling before saving.

Example names from the sample Chinese video:

```text
顧小糖
陸擎淵
```

- Enter only the names, as shown above. Numbers, quotation marks, and Korean explanations are unnecessary.
- Use names that actually appear in the current stream. Update them as needed when switching to another stream.
- You can enter up to **32 names**, **48 characters per name**, and **512 characters in total**.
- Leave the field blank to recognize speech without hints. Entering a name does not guarantee that it will always be recognized correctly.

## Caption position

- The default is **Show in browser**. For popups without menus, use **Show on monitor**.
- Enable **Edit caption position and size**, then drag or resize the captions. Their position and size are saved for the next launch. Disable editing when you have finished.
- Clicking **Show in browser / Show on monitor** restores the default position and size.
- The default monitor position is horizontally centered, with the bottom of the captions at 95% of the screen height.

## Verified model downloads

These models have been checked for successful loading and inference in this app. Select one translation model at a time.

| Purpose | Verified model | Download link |
|---|---|---|
| Speech recognition | Qwen3-ASR-1.7B | [Model folder](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf/tree/bcd2b5b7f32b480ab5790554cfa8347f246a14f3) |
| Speech recognition | Whisper large-v3-turbo · faster-whisper format | [Model folder](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/tree/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf) |
| Translation into Korean | HY-MT2-7B · Q6_K | [Download GGUF file](https://huggingface.co/tencent/Hy-MT2-7B-GGUF/resolve/ab8472660ac61fac25f1af43fac2599d52a8a775/HY-MT2-7B-Q6_K.gguf?download=true) |
| Translation into Korean | MiLMMT-46-12B-v1.0 · i1-Q4_K_M | [Download GGUF file](https://huggingface.co/mradermacher/MiLMMT-46-12B-v1.0-i1-GGUF/resolve/0d63dcbbc3e011bb2728c36769b991cd0109a2dc/MiLMMT-46-12B-v1.0.i1-Q4_K_M.gguf?download=true) |
| Translation into Korean | TranslateGemma 12B · Q4_K_M | [Download GGUF file](https://huggingface.co/bullerwins/translategemma-12b-it-GGUF/resolve/d7d1d8cc4ff53d4bc883ef33eae3894f07833b63/translategemma-12b-it-Q4_K_M.gguf?download=true) |
| Japanese-to-Korean translation only | ja-ko-vn-12b-v2 · Q4_K_M | [GGUF file list](https://huggingface.co/hell0ks/ja-ko-vn-12b-v2-gguf/tree/main) |

**For JA-KO-VN, set the input language to Japanese (or automatic detection) and the translation language to Korean.** Keeping the original filename, such as `ja-ko-vn-12b-v2-Q4_K_M.gguf`, lets the app automatically select the dedicated translation method that sends only the Japanese source text. For other language pairs, use a multilingual model such as HY-MT2. The table lists models whose loading and output have been checked; it does not rank their accuracy.

## Where to put model files

```text
LiveSubtitle/
├─ LiveSubtitle.exe
├─ models/
│  ├─ translation/            Translation .gguf files
│  └─ asr/
│     ├─ qwen3-asr-1.7b/      Qwen model, configuration, and tokenizer files
│     └─ whisper-large-v3-turbo/  Whisper model, configuration, and tokenizer files
└─ 0_UserManual(사용자매뉴얼)/
   ├─ 사용자매뉴얼.md          Korean
   ├─ UserManual.md            English
   ├─ 用户手册.md              Chinese
   └─ ユーザーマニュアル.md    Japanese
```

- Place translation GGUF files in `models/translation`, or use **Add model (GGUF)** in the app. **Keep the original filename**, because the filename determines which dedicated translation method is selected.
- A GGUF file appearing in the list does not guarantee that its translation format is supported. Dedicated translation models outside the verified list may need additional app support for their required input format.
- The supporting files in the Qwen and Whisper folders match the pinned versions linked in the table. If you use a different version or model, also provide the configuration and tokenizer files that version requires. Whisper's `model.bin` is used for default recognition; a `.pt` file is needed separately only for the experimental Whisper AlignAtt feature.
- After adding a model, restart the app or click **Reconnect**, then select it from the list. MiLMMT and HY-MT2 do not require a separate setting to disable thinking mode.
- To move to another PC, copy the entire app folder. You may omit the large files in `models` and download them again using the links above.
- The latest 100 translation records are saved in `logs/caption-history.json`, including the original text, translation, input language, and translation language.
