# flacblind
The `flacblind` utility allows you to perform an ABX blind test to distinguish between FLAC (lossless) and MP3 (lossy) audio files.

## Prerequisites

Ensure you have the following system dependencies installed:
- `python3`
- `ffmpeg` (for audio conversion and normalization)
- `paplay` (PulseAudio) or `aplay` (ALSA) for playback

## Usage

Run the tool by providing paths to the FLAC and MP3 versions of the same track:

```bash
python3 flacblind.py --mp3path /path/to/song.mp3 --flacpath /path/to/song.flac
```

## How it works

1. **Normalization**: The tool automatically converts both files to WAV and enforces the same loudness (using ffmpeg's `loudnorm`) to ensure a fair volume comparison.
2. **Game Loop**:
    - There are 10 rounds.
    - In each round, Sample A and Sample B are randomly assigned to either the FLAC or MP3 source.
    - You can play each sample as many times as you like.
    - Press **S** or **Space** to stop playback.
    - Press **1** if you think Sample A is the FLAC file.
    - Press **2** if you think Sample B is the FLAC file.
3. **Results**: After 10 rounds, your score is displayed.

## Keyboard Controls

- `A`: Play Sample A
- `B`: Play Sample B
- `S` / `Space`: Stop playback
- `1`: Vote for A as FLAC
- `2`: Vote for B as FLAC
- `Q`: Quit
