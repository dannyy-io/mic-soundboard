# Mic Soundboard

Play any audio file (mp3, wav, ogg, flac) through your mic into Discord calls. People hear your voice and the sounds together.

```
your mic ───────┐
                ├─► Mic Soundboard ─► CABLE Input ══ virtual cable ══ CABLE Output ─► Discord
sound buttons ──┘         └─► your headphones (so you hear the sounds too)
```

## Install (Windows)

1. **Install VB-Audio Virtual Cable** (free): <https://vb-audio.com/Cable/>
   Download the Windows package, unzip it, right-click `VBCABLE_Setup_x64.exe` → **Run as administrator** → **Install Driver**. **Restart your PC.**
2. **Install Python 3.10+** from <https://www.python.org/downloads/>. On the first installer screen, tick **"Add python.exe to PATH"**.
3. Download this repo (green **Code** button → **Download ZIP**), unzip it, and double-click **`Install.bat`**.
   It installs to `AppData\Local\Programs\Mic Soundboard`, adds **Start Menu and desktop icons**, and opens the app.

After that, open it from the Start Menu or the desktop icon. You can delete the zip and the unzipped folder.

**It updates itself.** Each time you open it, it checks this GitHub repo and installs any newer version first. While it's open it checks every few hours. When an update is ready, a green bar appears and you click **Restart & update** when it suits you. It never restarts on its own in the middle of a call. If an update ever fails to start, it goes back to the previous version automatically. Turn this off under Help → Update automatically. To remove the app, run `Uninstall.bat` in its folder.

## First-time setup

1. In the soundboard window:
   - **Your microphone** → your real mic
   - **Send to (virtual cable)** → `CABLE Input (VB-Audio Virtual Cable)` (usually picked for you)
   - **Hear sounds on** → your headphones/speakers (or `(off)`)
   - It starts by itself once the cable is picked. Otherwise press **Start**.
2. **In Discord** → User Settings → Voice & Video:
   - **Input Device** → `CABLE Output (VB-Audio Virtual Cable)`
   - **Noise Suppression** → **None** (Krisp treats music as noise and cuts it out)
   - Turn **off** Echo Cancellation and Automatic Gain Control
   - Input mode: **Voice Activity** with "Automatically determine input sensitivity" off and the slider set low, so quiet parts of songs don't get cut off.
     (Push to Talk works too, but you'd have to hold the key while a sound plays.)

Discord's "Let's Check" mic test should now show your voice and any sound you click.

## Using it

- **Add sounds / Add folder** to load files. Click a button to play it into the call.
- **Right-click a sound** to set a global **hotkey** (it works while Discord or a game is focused), rename, reorder or remove it.
- **Right-click "Stop all"** to give stopping its own hotkey.
- **One sound at a time** stops the current sound when you start another one.
- **Your mic** checkbox mutes your voice but still sends sounds.
- **Noise suppression** removes keyboard clicks, mouse clicks and background noise from your voice only. Your sounds aren't touched. Keep Discord's own Noise Suppression on **None**, because it can't tell your sounds apart from noise. The first time you turn it on, it downloads a small filter (about 13 MB).
- Your sounds list, hotkeys, volumes and devices are saved in the app's `data` folder, and updates never touch it. (Help → Open app folder.)

## Playing Spotify / YouTube / anything else into the call

Windows Settings → System → Sound → **Volume mixer**. Find the app (Spotify, Chrome…) and set its **Output device** to `CABLE Input`. It gets mixed in with your mic automatically.
You won't hear that app yourself unless you also turn on "Listen to this device" for CABLE Output (Control Panel → Sound → Recording → CABLE Output → Properties → Listen). That also plays your own mic back to you, so it's only good with headphones.

## Troubleshooting

| Problem | Fix |
|---|---|
| No "CABLE Input" in the list | VB-Cable isn't installed or you haven't restarted. Click **Refresh** after installing. |
| "Couldn't open the audio devices" | Switch **Audio system** to `MME`. It works on nearly every PC. `Windows WASAPI` has lower delay if it works for you. |
| Friends hear music cutting in and out | Discord's Noise Suppression is still on, or Voice Activity sensitivity is too high. |
| Friends hear an echo of themselves | Your "Your microphone" is picking up your speakers. Use headphones. |
| Hotkeys don't work in a game running as admin | Right-click the Mic Soundboard icon → Run as administrator. |
| An mp3 won't load | Convert it to .wav or .ogg (any online converter works). |
| Your voice sounds robotic/crackly | Try the other Audio system, or close other audio apps that use exclusive mode. |
| Something else is wrong | Help → Open log file, and send that file to whoever is helping you. |
