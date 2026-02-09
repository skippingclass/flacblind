#!/usr/bin/env python3
import argparse
import random
import sys
import os
import termios
import tty
import subprocess
import tempfile
import shutil
import time

def get_key():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

class BlindTest:
    def __init__(self, mp3_path, flac_path):
        self.mp3_path = mp3_path
        self.flac_path = flac_path
        self.score = 0
        self.rounds = 10
        self.current_round = 0
        self.temp_dir = tempfile.mkdtemp()
        self.mp3_wav = os.path.join(self.temp_dir, "mp3_converted.wav")
        self.flac_wav = os.path.join(self.temp_dir, "flac_converted.wav")
        self.player_cmd = self._detect_player()
        self.audio_paths = {}

    def _detect_player(self):
        if shutil.which("paplay"):
            return "paplay"
        elif shutil.which("aplay"):
            return "aplay"
        else:
            print("Error: No suitable audio player found (paplay or aplay required).")
            sys.exit(1)

    def convert_and_normalize(self):
        print("Converting and normalizing audio files...")
        
        # Normalize to -14 LUFS (standard for streaming)
        # Using loudnorm filter
        common_args = ["-filter:a", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100"]

        cmd_mp3 = ["ffmpeg", "-y", "-i", self.mp3_path] + common_args + [self.mp3_wav]
        cmd_flac = ["ffmpeg", "-y", "-i", self.flac_path] + common_args + [self.flac_wav]

        devnull = open(os.devnull, 'w')
        
        print(f"Processing MP3: {self.mp3_path}")
        subprocess.check_call(cmd_mp3, stdout=devnull, stderr=devnull)
        
        print(f"Processing FLAC: {self.flac_path}")
        subprocess.check_call(cmd_flac, stdout=devnull, stderr=devnull)
        
        self.audio_paths['mp3'] = self.mp3_wav
        self.audio_paths['flac'] = self.flac_wav

    def play_audio(self, path):
        # Stop existing playback logic is handled in run_round
        try:
            return subprocess.Popen([self.player_cmd, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"\nError playing audio: {e}")
            return None

    def run_round(self):
        self.current_round += 1
        print(f"\n--- Round {self.current_round}/{self.rounds} ---")
        
        # Randomize
        is_a_flac = random.choice([True, False])
        if is_a_flac:
            sample_a = self.audio_paths['flac']
            sample_b = self.audio_paths['mp3']
            correct_answer = 'A'
        else:
            sample_a = self.audio_paths['mp3']
            sample_b = self.audio_paths['flac']
            correct_answer = 'B'
            
        print("Press 'A' to play Sample A")
        print("Press 'B' to play Sample B")
        print("Press 'S' or 'Space' to Stop playback")
        print("Press '1' to guess A is FLAC")
        print("Press '2' to guess B is FLAC")
        print("Press 'Q' to quit")
        
        current_playback_proc = None
        
        while True:
            key = get_key()
            
            # Handle Ctrl+C
            if key == '\x03':
                if current_playback_proc and current_playback_proc.poll() is None:
                    current_playback_proc.terminate()
                self.cleanup()
                sys.exit(0)
                
            key = key.upper()
            
            if current_playback_proc and current_playback_proc.poll() is None:
                current_playback_proc.terminate()
                current_playback_proc = None
                
            if key == 'Q':
                self.cleanup()
                print("\nQuitting...")
                sys.exit(0)
            elif key == 'S' or key == ' ':
                print("\rStopped...", end='', flush=True)
                pass
            elif key == 'A':
                print("\rPlaying Sample A...", end='', flush=True)
                current_playback_proc = self.play_audio(sample_a)
            elif key == 'B':
                print("\rPlaying Sample B...", end='', flush=True)
                current_playback_proc = self.play_audio(sample_b)
            elif key == '1':
                if correct_answer == 'A':
                    print("\nCorrect!")
                    self.score += 1
                else:
                    print(f"\nWrong! A was {'FLAC' if is_a_flac else 'MP3'}, B was {'FLAC' if not is_a_flac else 'MP3'}")
                break
            elif key == '2':
                if correct_answer == 'B':
                    print("\nCorrect!")
                    self.score += 1
                else:
                    print(f"\nWrong! A was {'FLAC' if is_a_flac else 'MP3'}, B was {'FLAC' if not is_a_flac else 'MP3'}")
                break

    def cleanup(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def run(self):
        try:
            self.convert_and_normalize()
            
            input("Press Enter to start the test...")
            
            for _ in range(self.rounds):
                self.run_round()
                
            print(f"\nFinal Score: {self.score}/{self.rounds}")
            if self.score >= 8:
                print("Excellent ears!")
                print("You can reliably distinguish lossless audio!")
            elif self.score >= 6:
                print("Good job, but could be luck.")
            elif self.score >= 4:
                print("Hard to tell, isn't it?")
            else:
                print("You probably don't need FLAC files.")
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            self.cleanup()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blind test between FLAC and MP3")
    parser.add_argument("--mp3path", required=True, help="Path to MP3 file")
    parser.add_argument("--flacpath", required=True, help="Path to FLAC file")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.mp3path):
        print(f"File not found: {args.mp3path}")
        sys.exit(1)
    if not os.path.exists(args.flacpath):
        print(f"File not found: {args.flacpath}")
        sys.exit(1)
        
    test = BlindTest(args.mp3path, args.flacpath)
    test.run()
