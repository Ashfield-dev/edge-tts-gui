# --- START OF FILE final.py ---

# --- Imports ---
import customtkinter as ctk
import asyncio
import edge_tts
import edge_tts.exceptions
import tempfile
import os
import re
import threading
import time
from tkinter import filedialog
import json
# from mutagen.mp3 import MP3 # Option: Remove if no duration fallback planned
# from mutagen import MutagenError # Option: Remove if no duration fallback planned
try:
    from just_playback import Playback
    JUST_PLAYBACK_AVAILABLE = True
except ImportError:
    print("ERROR: Required library 'just_playback' not found. Please install it: pip install just_playback")
    JUST_PLAYBACK_AVAILABLE = False
except Exception as e:
    # Catch other potential errors during just_playback import/initialization
    print(f"ERROR: Failed to import or initialize just_playback: {e}")
    JUST_PLAYBACK_AVAILABLE = False


# --- Configuration ---
# Set initial mode to follow the system setting
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue") # Options: "blue", "green", "dark-blue"

# --- Constants ---
AUDIO_UPDATE_INTERVAL_MS = 100 # Progress bar update interval (ms)
MIN_WINDOW_WIDTH = 700
MIN_WINDOW_HEIGHT = 650
SEEK_INTERVAL_SECONDS = 5 # Number of seconds to jump forward/backward
# DEFAULT_APPEARANCE_MODE = "Light" # REMOVED - Now starts with "System"
TEXTBOX_PLACEHOLDER_TEXT = "Enter text here or load from a file..."
# Choose a placeholder color that works reasonably well in both light/dark modes
TEXTBOX_PLACEHOLDER_COLOR = "#888888" # Medium-Gray
CONFIG_PATH = "ui_state.json"


class StoredUiState:
    def __init__(self, rate: int = 0, pitch: int = 0, voice: str = None, dark: bool = None):
        if dark is None:
            dark = ctk.get_appearance_mode() == "Dark"
        self.rate = rate
        self.pitch = pitch
        self.voice = voice
        self.dark = dark

# --- Main Application ---
class EdgeTTSApp(ctk.CTk):
    """
    GUI application to generate Text-to-Speech using Microsoft Edge TTS
    and play it back using the just_playback library.
    Follows system theme initially, with a toggle override.
    Includes Textbox placeholder simulation.
    """
    def __init__(self):
        """Initializes the main window, UI elements, and application state."""
        # NOTE: Appearance mode ("System") is set *before* initializing CTk object
        super().__init__()

        # --- Theme is now initially System ---
        print(f"INFO: Initial appearance mode requested: 'System'")

        self.title("Edge TTS Text-to-Speech")
        self.resizable(True, True)
        self.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        # Initialize Audio Player
        self.player: Playback | None = None
        self.just_playback_initialized: bool = False
        if JUST_PLAYBACK_AVAILABLE:
            try:
                self.player = Playback()
                self.just_playback_initialized = True
                print("INFO: just_playback initialized successfully.")
            except Exception as e:
                print(f"ERROR: Failed to initialize just_playback: {e}")
                self.just_playback_initialized = False

        # Application State
        self.voices_dict: dict[str, str] = {} # {Display Name: ShortName}
        self._all_voice_display_names: list[str] = []
        self.audio_file_path: str | None = None # Path to the temporary audio file
        self.audio_duration: float = 0.0 # Audio duration in seconds
        self._after_id_update_progress: str | None = None # ID for the 'after' job updating progress
        self._slider_being_dragged: bool = False # Flag if user is dragging the progress slider

        # Placeholder state
        self.textbox_placeholder_active = False
        self.default_textbox_color = None # Will be fetched after widget creation

        ui_state = self.load_ui_state()
        self._build_ui(ui_state) # Build the UI

        # Initial Actions
        self.after(10, self._fetch_default_textbox_color) # Schedule fetching color early
        self.after(20, self._set_initial_textbox_placeholder) # Set initial placeholder state after color fetch attempt

        if self.just_playback_initialized:
             self.update_status("Loading voices..."); self.load_voices_async(ui_state)
        else:
             self.update_status("❌ Error: Audio library init failed. Audio disabled.");
             self.set_ui_state('error_no_audio')

        # Set initial state of the theme switch based on the *actual* mode determined by "System"
        # Needs a slight delay for the system mode to be resolved and applied
        self.after(50, self._update_theme_switch_state)
        print(f"INFO: Actual initial mode (after System resolution): '{ctk.get_appearance_mode()}'")


    def _build_ui(self, ui_state: StoredUiState = StoredUiState()):
        """Creates all user interface elements (widgets)."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1) # Textbox row expands
        self.grid_rowconfigure(3, weight=0) # Controls row fixed
        self.grid_rowconfigure(5, weight=0) # Player row fixed

        # --- Input Area ---
        input_header_frame = ctk.CTkFrame(self, fg_color="transparent")
        input_header_frame.grid(row=0, column=0, padx=20, pady=(10, 0), sticky="ew")
        input_header_frame.grid_columnconfigure(0, weight=1) # Label expands
        ctk.CTkLabel(input_header_frame, text="Input Text", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w")

        # Theme Toggle Switch
        self.theme_switch = ctk.CTkSwitch(
            input_header_frame,
            text="Dark Mode",
            command=self._toggle_theme_override, # Changed command name
            onvalue=1,  # Represents "Dark" mode ON
            offvalue=0  # Represents "Dark" mode OFF (i.e., Light mode)
        )
        self.theme_switch.grid(row=0, column=1, padx=(10, 5), sticky="e")

        if ui_state.dark:
            self.theme_switch.select()
            ctk.set_appearance_mode("dark")
        else:
            self.theme_switch.deselect()
            ctk.set_appearance_mode("light")

        self.load_file_btn = ctk.CTkButton(input_header_frame, text="Load File...", width=100, command=self.load_text_from_file)
        self.load_file_btn.grid(row=0, column=2, padx=(0, 5), sticky="e")


        input_frame = ctk.CTkFrame(self)
        input_frame.grid(row=1, column=0, padx=20, pady=5, sticky="nsew")
        input_frame.grid_rowconfigure(0, weight=1); input_frame.grid_columnconfigure(0, weight=1)

        # --- Textbox Setup ---
        self.textbox = ctk.CTkTextbox(input_frame, wrap="word")
        self.textbox.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")

        # Bind focus events for placeholder simulation
        self.textbox.bind("<FocusIn>", self._on_textbox_focus_in)
        self.textbox.bind("<FocusOut>", self._on_textbox_focus_out)

        # Detect text changes
        self.textbox.bind("<KeyRelease>", self._on_textbox_change)

        # --- Controls Area (Voice & Adjustments) ---
        # [Rest of the controls setup remains the same as before]
        ctk.CTkLabel(self, text="Voice & Adjustments", font=ctk.CTkFont(size=14, weight="bold")).grid(row=2, column=0, padx=20, pady=(10, 0), sticky="w")
        controls_frame = ctk.CTkFrame(self)
        controls_frame.grid(row=3, column=0, padx=20, pady=5, sticky="ew")
        controls_frame.grid_columnconfigure(0, weight=1, uniform="group1"); controls_frame.grid_columnconfigure(1, weight=2, uniform="group1")
        voice_select_frame = ctk.CTkFrame(controls_frame); voice_select_frame.grid(row=0, column=0, padx=(0, 5), pady=5, sticky="nsew")
        voice_select_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(voice_select_frame, text="Select Voice", font=ctk.CTkFont(size=12)).grid(row=0, column=0, padx=5, pady=(5, 2), sticky="w")
        self.voice_search_entry = ctk.CTkEntry(voice_select_frame, placeholder_text="Search voice..."); self.voice_search_entry.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="ew")
        self.voice_search_entry.bind("<KeyRelease>", self._on_voice_search)
        self.voice_dropdown = ctk.CTkComboBox(voice_select_frame, values=["Loading voices..."], state="disabled", command=self.voice_selected); self.voice_dropdown.grid(row=2, column=0, padx=5, pady=(0, 5), sticky="ew")
        adj_frame = ctk.CTkFrame(controls_frame); adj_frame.grid(row=0, column=1, padx=(5, 0), pady=5, sticky="nsew")
        adj_frame.grid_columnconfigure(0, weight=1)
        rate_adj_frame = ctk.CTkFrame(adj_frame); rate_adj_frame.grid(row=0, column=0, padx=5, pady=(5,2), sticky="ew")
        rate_adj_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(rate_adj_frame, text="Rate:", font=ctk.CTkFont(size=11)).grid(row=0, column=0, padx=(5,0), pady=5, sticky="w")
        self.rate_slider = ctk.CTkSlider(rate_adj_frame, from_=-100, to=100, number_of_steps=40, command=self.update_rate_label); self.rate_slider.set(ui_state.rate); self.rate_slider.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        self.rate_value_label = ctk.CTkLabel(rate_adj_frame, text=f"{ui_state.rate}%", width=40, anchor="e"); self.rate_value_label.grid(row=0, column=2, padx=(0, 5), pady=5, sticky="e")
        self.rate_reset_btn = ctk.CTkButton(rate_adj_frame, text="Reset", width=50, command=lambda: self.reset_slider("rate")); self.rate_reset_btn.grid(row=0, column=3, padx=(0,5), pady=5)
        pitch_adj_frame = ctk.CTkFrame(adj_frame); pitch_adj_frame.grid(row=1, column=0, padx=5, pady=(2,5), sticky="ew")
        pitch_adj_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(pitch_adj_frame, text="Pitch:", font=ctk.CTkFont(size=11)).grid(row=0, column=0, padx=(5,0), pady=5, sticky="w")
        self.pitch_slider = ctk.CTkSlider(pitch_adj_frame, from_=-50, to=50, number_of_steps=20, command=self.update_pitch_label); self.pitch_slider.set(ui_state.pitch); self.pitch_slider.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        self.pitch_value_label = ctk.CTkLabel(pitch_adj_frame, text=f"{ui_state.pitch}Hz", width=40, anchor="e"); self.pitch_value_label.grid(row=0, column=2, padx=(0, 5), pady=5, sticky="e")
        self.pitch_reset_btn = ctk.CTkButton(pitch_adj_frame, text="Reset", width=50, command=lambda: self.reset_slider("pitch")); self.pitch_reset_btn.grid(row=0, column=3, padx=(0,5), pady=5)

        # --- Generate Button ---
        self.generate_btn = ctk.CTkButton(self, text="Generate Speech", command=self.start_generate_speech_thread, height=40, font=ctk.CTkFont(size=14, weight="bold"), state="disabled")
        self.generate_btn.grid(row=4, column=0, padx=20, pady=5, sticky="ew")

        # --- Player Controls ---
        # [Player controls setup remains the same as before]
        self.player_frame = ctk.CTkFrame(self)
        self.player_frame.grid(row=5, column=0, padx=20, pady=5, sticky="ew")
        self.player_frame.grid_columnconfigure(4, weight=1) # Progress slider column expands
        self.rewind_btn = ctk.CTkButton(self.player_frame, text=f"<< {SEEK_INTERVAL_SECONDS}s", width=60, command=lambda: self.seek_relative(-SEEK_INTERVAL_SECONDS), state="disabled")
        self.rewind_btn.grid(row=0, column=0, padx=(10, 5), pady=10)
        self.play_pause_btn = ctk.CTkButton(self.player_frame, text="▶ Play", width=80, command=self.toggle_play_pause, state="disabled")
        self.play_pause_btn.grid(row=0, column=1, padx=5, pady=10)
        self.stop_btn = ctk.CTkButton(self.player_frame, text="⏹ Stop", width=80, command=self.stop_audio, state="disabled")
        self.stop_btn.grid(row=0, column=2, padx=5, pady=10)
        self.forward_btn = ctk.CTkButton(self.player_frame, text=f"{SEEK_INTERVAL_SECONDS}s >>", width=60, command=lambda: self.seek_relative(SEEK_INTERVAL_SECONDS), state="disabled")
        self.forward_btn.grid(row=0, column=3, padx=5, pady=10)
        self.progress_slider = ctk.CTkSlider(self.player_frame, from_=0, to=100, state="disabled")
        self.progress_slider.set(0)
        self.progress_slider.grid(row=0, column=4, padx=5, pady=10, sticky="ew")
        self.progress_slider.bind("<ButtonRelease-1>", self.seek_audio_on_release) # On slider release
        self.progress_slider.bind("<ButtonPress-1>", self.pause_updates_on_drag)   # On slider press
        self.time_label = ctk.CTkLabel(self.player_frame, text="00:00 / 00:00", width=90, font=ctk.CTkFont(size=10), anchor="e")
        self.time_label.grid(row=0, column=5, padx=(0, 10), pady=10, sticky="e")

        # --- Save Button ---
        self.save_btn = ctk.CTkButton(self, text="Save Audio as MP3", command=self.save_audio, height=40, font=ctk.CTkFont(size=14), state="disabled")
        self.save_btn.grid(row=6, column=0, padx=20, pady=5, sticky="ew")

        # --- Status Label ---
        self.status_label = ctk.CTkLabel(self, text="Status: Initializing...", height=25, anchor="w")
        self.status_label.grid(row=7, column=0, padx=20, pady=(5, 10), sticky="ew")

    # --- Textbox Placeholder Logic ---
    def _fetch_default_textbox_color(self):
        """Fetches and stores the default text color of the textbox."""
        if hasattr(self, 'textbox') and self.textbox.winfo_exists():
            try:
                # Ensure the widget is fully ready
                self.textbox.update_idletasks()
                self.default_textbox_color = self.textbox.cget("text_color")
                print(f"INFO: Default textbox color fetched: {self.default_textbox_color}")
                # If placeholder is currently active, ensure its color is correct
                # This can happen if fetch was delayed past initial set
                if self.textbox_placeholder_active:
                    self.textbox.configure(text_color=TEXTBOX_PLACEHOLDER_COLOR)

            except Exception as e:
                print(f"WARN: Could not fetch default textbox color: {e}")
                # Fallback (might not match theme perfectly)
                current_mode = ctk.get_appearance_mode()
                self.default_textbox_color = "#DCE4EE" if current_mode == "Dark" else "#111111" # Near-white for Dark, Near-black for Light
                print(f"INFO: Using fallback textbox color for {current_mode} mode: {self.default_textbox_color}")
        else:
            # Reschedule if textbox doesn't exist or color isn't ready yet
             self.after(50, self._fetch_default_textbox_color)

    def load_ui_state(self) -> StoredUiState:
        """Loads audio settings from a JSON configuration file or returns defaults."""
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    defaults = StoredUiState()
                    data = json.load(f)
                    rate = int(data.get("rate", defaults.rate))
                    pitch = int(data.get("pitch", defaults.pitch))
                    voice = data.get("voice", defaults.voice)
                    dark = data.get("dark", defaults.dark)
                    return StoredUiState(rate=rate, pitch=pitch, voice=voice, dark=dark)
        except Exception as e:
            print(f"WARN: Failed to load audio settings from JSON: {e}")
        return StoredUiState()  # Default settings if file missing or error

    def _set_initial_textbox_placeholder(self):
        """Sets the placeholder text and color if the textbox is empty."""
        if not hasattr(self, 'textbox') or not self.textbox.winfo_exists():
            self.after(50, self._set_initial_textbox_placeholder) # Retry if widget not ready
            return
        # Ensure default color is fetched before proceeding
        if self.default_textbox_color is None:
            print("INFO: Waiting for default text color fetch before setting placeholder...")
            self.after(50, self._set_initial_textbox_placeholder) # Retry shortly
            return

        current_text = self.textbox.get("1.0", "end-1c").strip()
        if not current_text:
            self.textbox_placeholder_active = True
            self.textbox.insert("1.0", TEXTBOX_PLACEHOLDER_TEXT)
            self.textbox.configure(text_color=TEXTBOX_PLACEHOLDER_COLOR)
            print("INFO: Initial textbox placeholder set.")


    def _on_textbox_focus_in(self, event=None):
        """Handles the textbox gaining focus."""
        if not hasattr(self, 'textbox') or not self.default_textbox_color: return
        if self.textbox_placeholder_active:
            self.textbox_placeholder_active = False
            self.textbox.delete("1.0", ctk.END)
            self.textbox.configure(text_color=self.default_textbox_color)

    def _on_textbox_focus_out(self, event=None):
        """Handles the textbox losing focus."""
        if not hasattr(self, 'textbox') or not self.default_textbox_color: return
        # Use after_idle to allow potential other focus-out events to process first
        self.after_idle(self._check_and_set_placeholder)

    def _check_and_set_placeholder(self):
        """Checks if textbox is empty after focus-out and sets placeholder."""
        if not hasattr(self, 'textbox') or not self.default_textbox_color: return
        # Verify focus is actually lost from textbox (important!)
        if self.focus_get() != self.textbox:
            current_text = self.textbox.get("1.0", "end-1c").strip()
            if not current_text:
                self.textbox_placeholder_active = True
                self.textbox.insert("1.0", TEXTBOX_PLACEHOLDER_TEXT)
                self.textbox.configure(text_color=TEXTBOX_PLACEHOLDER_COLOR)

    def get_input_text(self) -> str:
        """Gets the text from the textbox, excluding the placeholder."""
        if not hasattr(self, 'textbox'):
            return ""
        if self.textbox_placeholder_active:
            return ""
        else:
            return self.textbox.get("1.0", "end-1c").strip()

    # --- Theme Toggle ---
    def _toggle_theme_override(self):
        """
        Switches the appearance mode explicitly to Light or Dark,
        overriding the initial "System" setting.
        """
        if hasattr(self, 'theme_switch'):
            switch_state = self.theme_switch.get() # 1 for ON (Dark), 0 for OFF (Light)
            new_mode = "Dark" if switch_state == 1 else "Light"
            # Explicitly set the mode, stopping system following
            ctk.set_appearance_mode(new_mode)
            print(f"INFO: Appearance mode explicitly set to '{new_mode}' (overriding System).")

            # Update default color after theme change (needs a slight delay)
            if hasattr(self, 'textbox'):
                 self.after(50, self._update_textbox_colors_after_theme_change)
        else:
            print("WARN: Theme switch not available.")

    def _update_textbox_colors_after_theme_change(self):
        """Updates textbox color refs and re-applies placeholder if needed."""
        print("INFO: Updating textbox colors after theme change...")
        self._fetch_default_textbox_color() # Re-fetch the potentially new default color
        # The fetch function now handles applying placeholder color if active

    def _update_theme_switch_state(self):
        """Sets the theme switch state based on the *current effective* appearance mode."""
        if hasattr(self, 'theme_switch') and self.theme_switch.winfo_exists():
            try:
                # Use get_appearance_mode() which returns the resolved mode ("Light" or "Dark")
                current_mode = ctk.get_appearance_mode()
                print(f"INFO: Updating theme switch state for effective mode: '{current_mode}'")
                if current_mode == "Dark":
                    self.theme_switch.select() # Turn switch ON
                else:
                    self.theme_switch.deselect() # Turn switch OFF
            except Exception as e:
                 print(f"WARN: Error updating theme switch state: {e}")
        else:
            # Reschedule if switch doesn't exist yet
            self.after(100, self._update_theme_switch_state)
            print("WARN: Theme switch not available for state update yet, rescheduling.")

    # --- UI & State Update Methods ---
    def update_status(self, message: str):
        """Updates the text in the bottom status bar."""
        if hasattr(self, 'status_label'):
            self.status_label.configure(text=f"Status: {message}")

    def _on_textbox_change(self, event=None):
        """Called when text is typed in the textbox to update UI state."""
        # Use after_idle to ensure the text change is processed first
        self.after_idle(self._update_ui_after_text_change)

    def _update_ui_after_text_change(self):
        """Updates UI state after text changes, maintaining current state context."""
        if not hasattr(self, 'textbox'):
            return

        current_state = self.check_current_audio_state()

        # Update UI state which will check text and enable/disable generate button
        self.set_ui_state(current_state)

    def check_current_audio_state(self):
        """ Determine current state based on existing conditions."""
        current_state = 'idle'  # Default
        # Check if we have generated audio
        if self.audio_file_path and os.path.exists(self.audio_file_path):
            if self.just_playback_initialized and self.player:
                if self.player.playing:
                    current_state = 'playing'
                elif self.player.paused:
                    current_state = 'paused'
                else:
                    current_state = 'generated'
            else:
                current_state = 'generated'
        return current_state

    def set_ui_state(self, state: str):
        """Sets the enabled/disabled state of UI widgets based on application state."""
        is_player_ready = bool(self.just_playback_initialized and self.player)
        is_audio_loaded = bool(is_player_ready and self.audio_file_path and os.path.exists(self.audio_file_path) and self.audio_duration > 0.001)

        is_playing = is_player_ready and self.player.playing
        is_paused = is_player_ready and self.player.paused
        is_idle = not is_playing and not is_paused # Idle/stopped condition

        # Determine capabilities based on state
        can_press_play_pause = is_audio_loaded and state not in ['generating', 'loading']
        can_stop = is_audio_loaded and (is_playing or is_paused)
        can_seek = is_audio_loaded and (is_playing or is_paused)
        can_save = is_audio_loaded and is_idle # Can save only when idle/stopped

        voices_loaded = bool(self.voices_dict)
        has_input_text = bool(self.get_input_text())

        # Add proper voice selection validation
        selected_voice = self.voice_dropdown.get() if hasattr(self, 'voice_dropdown') else ""
        has_valid_voice = (voices_loaded and
                           selected_voice and
                           selected_voice in self.voices_dict and
                           "Loading" not in selected_voice and
                           "No match" not in selected_voice)

        can_generate = has_valid_voice and has_input_text and state not in ['loading', 'generating', 'playing', 'error_no_audio']
        can_load_text = state not in ['loading', 'generating', 'playing', 'error_no_audio']
        controls_active = state not in ['loading', 'generating', 'error_no_audio']
        # Theme switch should always be active
        theme_switch_state = ctk.NORMAL

        # Determine widget states (ON/OFF)
        play_pause_btn_state = ctk.NORMAL if can_press_play_pause else ctk.DISABLED
        stop_btn_state = ctk.NORMAL if can_stop else ctk.DISABLED
        seek_btns_state = ctk.NORMAL if can_seek else ctk.DISABLED
        progress_slider_state = ctk.NORMAL if can_seek else ctk.DISABLED
        save_btn_state = ctk.NORMAL if can_save else ctk.DISABLED
        generate_btn_state = ctk.NORMAL if can_generate else ctk.DISABLED
        load_file_btn_state = ctk.NORMAL if can_load_text else ctk.DISABLED
        voice_ctrl_state = ctk.NORMAL if voices_loaded and controls_active else ctk.DISABLED
        adj_ctrl_state = ctk.NORMAL if controls_active else ctk.DISABLED
        # Textbox state should generally be normal unless globally disabled
        textbox_state = ctk.NORMAL if controls_active else ctk.DISABLED

        # Determine dynamic button texts
        generate_btn_text = "Generate Speech"
        if state == 'loading': generate_btn_text = "Loading Voices..."
        elif state == 'generating': generate_btn_text = "Generating..."
        elif state == 'error_no_audio': generate_btn_text = "Audio Error"

        play_pause_text = "▶ Play"
        if is_playing: play_pause_text = "⏸ Pause"
        elif is_paused: play_pause_text = "▶ Resume"

        # Apply states to widgets (use try-except for safety during init)
        try:
            # Use 'winfo_exists' for safety, especially during init/close
            if hasattr(self, 'theme_switch') and self.theme_switch.winfo_exists(): self.theme_switch.configure(state=theme_switch_state)
            if hasattr(self, 'voice_dropdown') and self.voice_dropdown.winfo_exists(): self.voice_dropdown.configure(state=voice_ctrl_state)
            if hasattr(self, 'voice_search_entry') and self.voice_search_entry.winfo_exists(): self.voice_search_entry.configure(state=voice_ctrl_state)
            if hasattr(self, 'rate_slider') and self.rate_slider.winfo_exists(): self.rate_slider.configure(state=adj_ctrl_state)
            if hasattr(self, 'pitch_slider') and self.pitch_slider.winfo_exists(): self.pitch_slider.configure(state=adj_ctrl_state)
            if hasattr(self, 'rate_reset_btn') and self.rate_reset_btn.winfo_exists(): self.rate_reset_btn.configure(state=adj_ctrl_state)
            if hasattr(self, 'pitch_reset_btn') and self.pitch_reset_btn.winfo_exists(): self.pitch_reset_btn.configure(state=adj_ctrl_state)
            if hasattr(self, 'textbox') and self.textbox.winfo_exists(): self.textbox.configure(state=textbox_state)
            if hasattr(self, 'load_file_btn') and self.load_file_btn.winfo_exists(): self.load_file_btn.configure(state=load_file_btn_state)
            if hasattr(self, 'generate_btn') and self.generate_btn.winfo_exists(): self.generate_btn.configure(state=generate_btn_state, text=generate_btn_text)
            if hasattr(self, 'save_btn') and self.save_btn.winfo_exists(): self.save_btn.configure(state=save_btn_state)

            if hasattr(self, 'play_pause_btn') and self.play_pause_btn.winfo_exists(): self.play_pause_btn.configure(state=play_pause_btn_state, text=play_pause_text)
            if hasattr(self, 'stop_btn') and self.stop_btn.winfo_exists(): self.stop_btn.configure(state=stop_btn_state)
            if hasattr(self, 'rewind_btn') and self.rewind_btn.winfo_exists(): self.rewind_btn.configure(state=seek_btns_state)
            if hasattr(self, 'forward_btn') and self.forward_btn.winfo_exists(): self.forward_btn.configure(state=seek_btns_state)
            if hasattr(self, 'progress_slider') and self.progress_slider.winfo_exists(): self.progress_slider.configure(state=progress_slider_state)
        except Exception as e:
            # This might happen during shutdown if widgets are destroyed
            if "application has been destroyed" not in str(e):
                 print(f"WARN: Error applying UI state '{state}': {e}")


    def reset_slider(self, slider_type: str):
        """Resets the Rate or Pitch slider to 0."""
        if slider_type == "rate":
            if hasattr(self, 'rate_slider'): self.rate_slider.set(0)
            self.update_rate_label(0)
        elif slider_type == "pitch":
            if hasattr(self, 'pitch_slider'): self.pitch_slider.set(0)
            self.update_pitch_label(0)

    def update_rate_label(self, value: float):
        """Updates the Rate percentage label."""
        if hasattr(self, 'rate_value_label'):
             self.rate_value_label.configure(text=f"{int(value):+d}%")

    def update_pitch_label(self, value: float):
        """Updates the Pitch Hertz label."""
        if hasattr(self, 'pitch_value_label'):
             self.pitch_value_label.configure(text=f"{int(value):+d}Hz")

    def voice_selected(self, choice: str):
        """Callback when a voice is selected from the dropdown. Updates the UI state."""
        current_state = self.check_current_audio_state()
        self.set_ui_state(current_state)

    def _filter_voices(self) -> list[str]:
        """Filters the list of voice display names based on search input."""
        if not hasattr(self, 'voice_search_entry'): return []
        search_term = self.voice_search_entry.get().lower()
        if not search_term: return self._all_voice_display_names # Return all if search is empty
        # Return names containing the search term (case-insensitive)
        return [name for name in self._all_voice_display_names if search_term in name.lower()]

    def _on_voice_search(self, event=None):
        """Updates the voice dropdown list as the user types in the search box."""
        if not hasattr(self, 'voice_dropdown'): return
        filtered_voices = self._filter_voices()
        current_selection = self.voice_dropdown.get()

        if not filtered_voices:
            # If no results, display message and disable dropdown
            self.voice_dropdown.configure(values=["No match found"], state=ctk.DISABLED)
            self.voice_dropdown.set("No match found")
            # Trigger UI update after setting invalid selection
            current_state = self.check_current_audio_state()
            self.set_ui_state(current_state)
        else:
            # If results found, update list and enable dropdown
            self.voice_dropdown.configure(values=filtered_voices, state=ctk.NORMAL)
            # Try to keep the current selection if it's still in the filtered list
            if current_selection in filtered_voices:
                self.voice_dropdown.set(current_selection)
            else: # Otherwise, select the first result
                self.voice_dropdown.set(filtered_voices[0])

            # Trigger UI update after setting new selection
            current_state = self.check_current_audio_state()
            self.set_ui_state(current_state)

    # --- Asynchronous Operations & Threading ---
    def load_voices_async(self, ui_state: StoredUiState = None):
        """Starts a thread to load the voice list asynchronously."""
        self.set_ui_state('loading')
        self.update_status("Loading voice list...")
        if ui_state:
            thread = threading.Thread(target=self._run_async_task, args=(self._load_voices_task, ui_state.voice), daemon=True)
        else:
            thread = threading.Thread(target=self._run_async_task, args=(self._load_voices_task,), daemon=True)
        thread.start()

    def start_generate_speech_thread(self):
        """Starts a thread to generate TTS audio asynchronously."""
        self._delete_temp_audio_file() # Delete old temp file first
        if self.just_playback_initialized and self.player and (self.player.playing or self.player.paused):
             self.stop_audio() # Stop playback if currently active

        # Use the dedicated function to get input text, ignoring placeholder
        text = self.get_input_text()
        selected_voice_display = self.voice_dropdown.get()

        # Input validation
        if not text: # Check if actual text is empty
            self.update_status("❌ Error: Text input is empty."); self.set_ui_state('idle'); return
        if not selected_voice_display or "Loading" in selected_voice_display or "No match" in selected_voice_display or selected_voice_display not in self.voices_dict:
            self.update_status("❌ Error: Please select a valid voice."); self.set_ui_state('idle'); return

        voice_short_name = self.voices_dict[selected_voice_display]
        rate = int(self.rate_slider.get())
        pitch = int(self.pitch_slider.get())
        rate_str = f"{rate:+d}%"
        pitch_str = f"{pitch:+d}Hz"

        self.set_ui_state('generating')
        self.update_status("Generating audio...")
        thread = threading.Thread(target=self._run_async_task,
                                  args=(self._generate_audio_task, text, voice_short_name, rate_str, pitch_str),
                                  daemon=True)
        thread.start()

    def _run_async_task(self, coro, *args):
        """Runs an asyncio coroutine in a new event loop (suitable for threads)."""
        try:
            asyncio.run(coro(*args))
        except Exception as e:
            print(f"ERROR: Exception in async task thread: {e}")
            # Update status on the main thread
            self.after(0, lambda: self.update_status(f"❌ Error during async operation: {e}"))
            self.after(0, lambda: self.set_ui_state('idle')) # Revert to idle state on error

    async def _load_voices_task(self, start_voice: str = None):
        """Coroutine to fetch the list of voices from edge-tts."""
        try:
            voices = await edge_tts.list_voices()
            # Sort by Locale, then ShortName for a structured display
            voices.sort(key=lambda v: (v['Locale'], v['ShortName']))
            # Create a dictionary for quick lookup {DisplayName: ShortName}
            self.voices_dict = {f"{v['FriendlyName']} ({v['Locale']}, {v['Gender']})": v['ShortName'] for v in voices}
            self._all_voice_display_names = list(self.voices_dict.keys())
            # Update the UI on the main thread when done
            self.after(0, self._update_voice_dropdown_ui, self._all_voice_display_names, start_voice)
        except Exception as e:
            print(f"ERROR: Failed to load voices: {e}")
            self.after(0, lambda: self.update_status(f"❌ Error loading voices: {e}"))
            self.after(0, lambda: self.set_ui_state('idle')) # Set to idle if loading fails

    def _update_voice_dropdown_ui(self, voice_list: list[str], start_voice: str = None):
        """Updates the voice ComboBox on the main thread."""
        if not hasattr(self, 'voice_dropdown') or not self.voice_dropdown.winfo_exists(): return

        if voice_list:
            self.voice_dropdown.configure(values=voice_list)
            if hasattr(self, 'voice_search_entry') and self.voice_search_entry.winfo_exists():
                 self.voice_search_entry.configure(state=ctk.NORMAL)
            self.update_status("Ready.")
            # Determine final state based on whether audio is already loaded
            current_state = 'generated' if self.audio_file_path else 'idle'
            self.set_ui_state(current_state)
            if start_voice and start_voice in voice_list:
                self.voice_dropdown.set(start_voice)
            else:
                self.voice_dropdown.set(voice_list[0]) # Select the first voice by default
        else:
            # If the list is empty (error during load)
            self.voice_dropdown.configure(values=["No voices found"], state=ctk.DISABLED)
            if hasattr(self, 'voice_search_entry') and self.voice_search_entry.winfo_exists():
                self.voice_search_entry.configure(state=ctk.DISABLED)
            self.update_status("❌ Error: No voices could be loaded.")
            self.set_ui_state('error_no_voices') # Specific error state

    async def _generate_audio_task(self, text: str, voice_short_name: str, rate_str: str, pitch_str: str):
        """Coroutine to generate audio and save it to a temporary file."""
        tmp_path = None
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice_short_name, rate=rate_str, pitch=pitch_str)
            # Create a temporary file (it won't be deleted automatically with delete=False)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", prefix="edge_tts_") as tmp_file:
                tmp_path = tmp_file.name

            # Save the audio stream from edge-tts to the created temp file
            await communicate.save(tmp_path)

            # Verify that the file was created and is not empty
            if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                self.audio_file_path = tmp_path # Store the path if valid
                print(f"INFO: Audio saved to temp file: {self.audio_file_path}")
                # Schedule the _on_audio_generated call on the main thread
                self.after(0, self._on_audio_generated)
            else:
                 # File is missing or empty
                 print(f"ERROR: Temp audio file missing or empty after generation: {tmp_path}")
                 if tmp_path and os.path.exists(tmp_path): # Attempt to remove if it exists
                     try: os.remove(tmp_path)
                     except OSError as rm_err: print(f"WARN: Could not remove invalid temp file {tmp_path}: {rm_err}")
                 self.audio_file_path = None
                 self.after(0, lambda: self.update_status("❌ Error: Failed to generate valid audio file."))
                 self.after(0, lambda: self.set_ui_state('idle'))

        except edge_tts.exceptions.NoAudioGeneratedError as e:
             # Specific error from edge-tts if no audio is produced (e.g., empty text)
             print(f"ERROR: Edge TTS reported no audio generated: {e}")
             if tmp_path and os.path.exists(tmp_path): # Clean up temp file if created
                 try: os.remove(tmp_path)
                 except OSError as rm_err: print(f"WARN: Could not remove temp file {tmp_path}: {rm_err}")
             self.audio_file_path = None
             # Use get_input_text for the message check
             self.after(0, lambda: self.update_status("❌ Error: No audio generated (is input text empty?)."))
             self.after(0, lambda: self.set_ui_state('idle'))
        except Exception as e:
            # Catch any other exceptions during generation
            print(f"ERROR: Exception during audio generation: {e}")
            if tmp_path and os.path.exists(tmp_path): # Attempt cleanup
                 try: os.remove(tmp_path)
                 except OSError as rm_err: print(f"WARN: Could not remove temp file {tmp_path}: {rm_err}")
            self.audio_file_path = None
            self.after(0, lambda: self.update_status(f"❌ Error generating audio: {e}"))
            self.after(0, lambda: self.set_ui_state('idle'))

    def _on_audio_generated(self):
        """Callback on the main thread after the temporary audio file is created."""
        print(f"INFO: Loading generated audio file: {self.audio_file_path}")
        if not self.just_playback_initialized or not self.player:
            self.update_status("❌ Error: Audio generated, but player is not ready."); self.set_ui_state('error_no_audio'); return
        if not self.audio_file_path or not os.path.exists(self.audio_file_path):
             self.update_status("❌ Error: Generated audio file path is invalid or missing."); self.set_ui_state('idle'); return

        try:
            # Stop the player if it's playing something else before loading the new file
            if self.player.playing or self.player.paused:
                self.player.stop()

            # Load the audio file into just_playback
            self.player.load_file(self.audio_file_path)
            # Add a small delay before getting duration, sometimes needed after load
            self.after(50, self._finish_audio_load)

        except Exception as e:
            # Catch errors during file loading *initiation* into just_playback
            print(f"ERROR: Failed to initiate loading audio file into player: {e}")
            self.update_status(f"❌ Error loading audio: {e}")
            self.audio_duration = 0
            self.set_ui_state('error_audio_format')
            self._delete_temp_audio_file() # Delete the problematic file


    def _finish_audio_load(self):
        """Gets duration and updates UI after just_playback has loaded the file."""
        if not self.just_playback_initialized or not self.player: return
        try:
            self.audio_duration = self.player.duration # Get duration from the player
            print(f"INFO: Audio file loaded. Duration: {self.audio_duration:.2f}s")

            # Check if the duration is valid
            if self.audio_duration > 0:
                self.update_status("✅ Audio generated! Press Play.")
                self.set_ui_state('generated') # State: ready to be played
                if hasattr(self, 'progress_slider') and self.progress_slider.winfo_exists():
                    self.progress_slider.set(0) # Reset slider
                self.update_time_label(0, self.audio_duration) # Update time label
            else:
                # Invalid duration (0 or negative)
                print(f"WARN: Audio file loaded but reports invalid duration ({self.audio_duration:.2f}s). File might be corrupted.")
                self.update_status("❌ Error: Audio file seems invalid (0 duration).")
                self.set_ui_state('error_audio_format') # Specific error state
                self._delete_temp_audio_file() # Delete the invalid file

        except Exception as e:
             # Catch errors getting duration or updating UI
             print(f"ERROR: Failed to finalize audio load (get duration/update UI): {e}")
             self.update_status(f"❌ Error finalizing audio load: {e}")
             self.audio_duration = 0
             self.set_ui_state('error_audio_format')
             self._delete_temp_audio_file()


    # --- Audio Playback Controls ---
    def toggle_play_pause(self):
        """Starts, pauses, or resumes audio playback."""
        if not self.just_playback_initialized or not self.player:
            self.update_status("❌ Error: Audio player not ready."); return
        # Need a valid audio file and duration > 0 to play/pause
        if not self.audio_file_path or not os.path.exists(self.audio_file_path) or self.audio_duration <= 0:
            # Attempt reload if file exists but duration is invalid (maybe previous load failed)
            if self.audio_file_path and os.path.exists(self.audio_file_path):
                 print("WARN: Audio has invalid duration, attempting reload...")
                 self._on_audio_generated() # Try the loading process again
                 # Note: _on_audio_generated is async in effect due to _finish_audio_load,
                 # so we can't immediately check duration here. Assume it will work or fail later.
                 return # Exit, let the reload process handle the state
            else:
                 self.update_status("❌ Error: No valid audio loaded."); return

        try:
            if self.player.playing:
                self.player.pause()
                self._stop_progress_updater() # Stop updates when paused
                self.set_ui_state('paused'); self.update_status("⏸ Audio paused.")
            elif self.player.paused:
                self.player.resume()
                self.set_ui_state('playing'); self.update_status("▶ Resuming audio...")
                self._start_progress_updater() # Resume updates
            else: # If not playing/paused (i.e., stopped or initial state)
                # Ensure seeked to start if stopped previously? just_playback usually resumes
                # self.player.seek(0) # Optional: uncomment to always start from beginning after stop
                self.player.play() # Start from last position (or beginning if stopped/newly loaded)
                self.set_ui_state('playing'); self.update_status("▶ Playing audio...")
                self._start_progress_updater() # Start progress updates
        except Exception as e:
            print(f"ERROR: Exception during toggle_play_pause: {e}")
            self.update_status(f"❌ Playback Error: {e}")
            self.set_ui_state('generated') # Revert to generated state on error

    def stop_audio(self):
        """Stops audio playback and resets position to the beginning."""
        if not self.just_playback_initialized or not self.player: return

        # Only stop if currently playing or paused
        if self.player.playing or self.player.paused:
            try:
                self.player.stop()
                self._stop_progress_updater()
                # Reset UI to initial position
                if hasattr(self, 'progress_slider') and self.progress_slider.winfo_exists():
                    self.progress_slider.set(0)
                self.update_time_label(0, self.audio_duration)
                self.set_ui_state('generated'); # State returns to 'ready to play'
                self.update_status("⏹ Audio stopped.")
            except Exception as e:
                print(f"ERROR: Exception during stop_audio: {e}")
                self.update_status(f"❌ Error stopping audio: {e}")
                self.set_ui_state('generated') # Still try to reset state
        else:
            # If already stopped, ensure UI is consistent
            self._stop_progress_updater()
            if hasattr(self, 'progress_slider') and self.progress_slider.winfo_exists():
                 self.progress_slider.set(0)
            self.update_time_label(0, self.audio_duration)
            self.set_ui_state('generated')

    # --- Progress Update & Seeking Logic ---
    def _start_progress_updater(self):
        """Starts the loop for updating the progress bar and time label."""
        self._stop_progress_updater() # Ensure any previous updater is stopped
        # Only start if player is ready and duration is valid
        if self.player and self.audio_duration > 0:
             # Schedule the first call to _update_progress after a short delay
             self._after_id_update_progress = self.after(AUDIO_UPDATE_INTERVAL_MS, self._update_progress)

    def _stop_progress_updater(self):
        """Stops the progress update loop."""
        if self._after_id_update_progress:
            try:
                 self.after_cancel(self._after_id_update_progress)
            except ValueError: # Can happen if ID is invalid (e.g., already cancelled)
                 pass
            except Exception as e:
                 # Catch TclError if app is closing
                 if "application has been destroyed" not in str(e):
                      print(f"WARN: Error cancelling progress updater: {e}")
            self._after_id_update_progress = None


    def pause_updates_on_drag(self, event=None):
        """Called when the user presses the progress slider."""
        if self._can_seek(): # Only set flag if seeking is possible
            self._slider_being_dragged = True
            # Optional: Could also stop the updater here if needed
            # self._stop_progress_updater()

    def _update_progress(self):
        """Method called periodically to update the progress UI."""
        # Safety check: Stop if player is not ready or window closing
        if not self.just_playback_initialized or not self.player or not self.winfo_exists():
            self._stop_progress_updater(); return

        # Update only if playing AND the user is not dragging the slider
        if self.player.playing and not self._slider_being_dragged:
            try:
                current_pos_sec = self.player.curr_pos
                total_duration = self.audio_duration

                # Ensure duration is valid before calculating percentage
                if total_duration > 0:
                    # Calculate progress percentage (0-100)
                    progress_percent = min(100, max(0, (current_pos_sec / total_duration) * 100))

                    # Check if slider exists before updating
                    slider_exists = hasattr(self, 'progress_slider') and self.progress_slider.winfo_exists()

                    # Update slider UI only if the value changed significantly (reduces flicker)
                    if slider_exists:
                        # Use try-except for slider access as it might be destroyed during close
                        try:
                             current_slider_val = self.progress_slider.get()
                             if abs(current_slider_val - progress_percent) > 0.5: # 0.5% tolerance
                                self.progress_slider.set(progress_percent)
                        except Exception as slider_e:
                              print(f"WARN: Error accessing slider during progress update: {slider_e}")
                              slider_exists = False # Assume slider is gone

                    # Update time label
                    self.update_time_label(current_pos_sec, total_duration)
                else:
                    # Abnormal condition if duration is 0 while playing, stop the updater
                    print("WARN: Invalid duration detected during progress update.")
                    self._stop_progress_updater()
                    if hasattr(self, 'progress_slider') and self.progress_slider.winfo_exists():
                         try: self.progress_slider.set(0)
                         except: pass # Ignore errors if closing
                    self.update_time_label(0, 0)
                    return # Do not reschedule

                # Reschedule the next update call ONLY if still playing
                # Check player state *again* as it might have finished between checks
                if self.player.playing:
                   # Check window exists before scheduling next 'after'
                   if self.winfo_exists():
                        self._after_id_update_progress = self.after(AUDIO_UPDATE_INTERVAL_MS, self._update_progress)
                   else:
                        self._after_id_update_progress = None # Window closed
                else:
                    # Playback finished naturally (detected by state check)
                    print("INFO: Playback finished naturally.")
                    self._stop_progress_updater()
                    # Call stop_audio after a short delay to reset UI/state if window still exists
                    if self.winfo_exists():
                         self.after(50, self.stop_audio) # Increased delay slightly

            except Exception as e:
                # Catch errors during the update process
                # Check if error is due to closing window
                if "application has been destroyed" not in str(e) and "invalid command name" not in str(e):
                     print(f"ERROR: Exception in progress update loop: {e}")
                self._stop_progress_updater() # Stop updater on error

        # If not playing or being dragged, ensure updater is stopped
        elif (not self.player.playing or self._slider_being_dragged) and self._after_id_update_progress:
             self._stop_progress_updater()


    def format_time(self, seconds: float) -> str:
        """Formats seconds into an MM:SS string."""
        if not isinstance(seconds, (int, float)) or seconds < 0: seconds = 0
        minutes = int(seconds // 60)
        seconds = int(seconds % 60)
        return f"{minutes:02d}:{seconds:02d}"

    def update_time_label(self, current_seconds: float, total_seconds: float):
        """Updates the 'MM:SS / MM:SS' time label."""
        # Ensure non-negative inputs
        current_seconds = max(0, current_seconds if isinstance(current_seconds, (int, float)) else 0)
        total_seconds = max(0, total_seconds if isinstance(total_seconds, (int, float)) else 0)
        # Format times
        current_time_str = self.format_time(current_seconds)
        total_time_str = self.format_time(total_seconds)
        # Update the label if it exists
        if hasattr(self, 'time_label') and self.time_label.winfo_exists():
            try:
                 self.time_label.configure(text=f"{current_time_str} / {total_time_str}")
            except Exception as e:
                 # Ignore TclError if widget destroyed during update
                 if "application has been destroyed" not in str(e):
                      print(f"WARN: Error updating time label: {e}")

    def _can_seek(self) -> bool:
        """Checks if the current conditions allow seeking."""
        # Check player state too
        player_ready = self.just_playback_initialized and self.player and (self.player.playing or self.player.paused)
        return (player_ready
                and self.audio_file_path and os.path.exists(self.audio_file_path) # Check file path
                and self.audio_duration > 0.001)


    def _perform_seek(self, target_seek_time_sec: float):
        """Core logic for seeking: stop updater, seek, update UI, schedule updater restart."""
        if not self._can_seek(): return # Do nothing if seeking isn't possible

        try:
            was_playing = self.player.playing # Remember if it was playing before seek
            self._stop_progress_updater() # Stop updater first

            # Clamp target time to valid duration range (allow seeking very close to end)
            target_seek_time_sec = max(0, min(target_seek_time_sec, self.audio_duration - 0.01)) # Subtract tiny amount

            # Perform seek using just_playback
            self.player.seek(target_seek_time_sec)

            # Schedule immediate UI update after a short delay (allows seek to register)
            # Ensure window exists before scheduling 'after' calls
            if self.winfo_exists():
                 self.after(30, self._update_ui_after_seek_internal)
                 # Schedule the updater restart check only if it was playing before
                 if was_playing:
                     self.after(80, self._maybe_restart_updater) # Slightly longer delay
            else:
                 print("WARN: Window closed during seek operation, skipping UI updates.")

        except Exception as e:
             # Catch errors during the seek operation
             print(f"ERROR: Exception during seek operation: {e}")
             self.update_status(f"❌ Error seeking: {e}")
             # Still try to schedule updater restart check if it was playing
             if self.winfo_exists() and was_playing:
                 self.after(80, self._maybe_restart_updater)


    def seek_relative(self, seconds_to_add: int):
        """Jumps forward or backward by a specified number of seconds."""
        if not self._can_seek(): return
        current_pos_sec = self.player.curr_pos
        target_seek_time_sec = current_pos_sec + seconds_to_add
        self._perform_seek(target_seek_time_sec) # Use the helper

    def seek_audio_on_release(self, event=None):
        """Called when the user releases the click on the progress slider."""
        if not self._can_seek(): # Check if seeking is possible *before* clearing the flag
             self._slider_being_dragged = False # Always clear flag on release
             return
        if not hasattr(self, 'progress_slider'):
             self._slider_being_dragged = False
             return

        seek_percent = self.progress_slider.get()
        # Clear the flag *before* performing the seek
        self._slider_being_dragged = False
        # Calculate target time based on slider percentage
        target_seek_time_sec = (seek_percent / 100.0) * self.audio_duration
        self._perform_seek(target_seek_time_sec) # Use the helper

    def _update_ui_after_seek_internal(self):
        """Updates the slider position and time label immediately after a seek."""
        if not self.just_playback_initialized or not self.player or not self.winfo_exists(): return
        # Update only if player is in a valid state (playing/paused)
        if self.player.playing or self.player.paused:
            try:
                 current_pos = self.player.curr_pos
                 duration = self.audio_duration
                 self.update_time_label(current_pos, duration) # Update time label
                 if duration > 0 and hasattr(self, 'progress_slider') and self.progress_slider.winfo_exists():
                      # Update slider position
                      percent = min(100, max(0, (current_pos / duration) * 100))
                      self.progress_slider.set(percent)
            except Exception as e:
                 # Catch errors during post-seek UI update
                 if "application has been destroyed" not in str(e):
                      print(f"ERROR: Exception updating UI after seek: {e}")

    def _maybe_restart_updater(self):
        """Checks conditions and restarts the progress updater if necessary."""
        if not self.just_playback_initialized or not self.player or not self.winfo_exists(): return
        # Restart only if: playing, NOT dragging, AND updater is not already running
        if self.player.playing and not self._slider_being_dragged and not self._after_id_update_progress:
             self._start_progress_updater()

    # --- File Operations (Load Text, Save Audio) ---
    def load_text_from_file(self):
        """Opens a dialog to select a text (.txt or .srt) file and loads its content."""
        file_path = filedialog.askopenfilename(
            title="Select Text or Subtitle File", # Dialog title
            filetypes=[("Text files", "*.txt"), ("SubRip Subtitles", "*.srt"), ("All files", "*.*")] # File type filters
        )
        if not file_path:
            self.update_status("File selection cancelled."); return # User cancelled

        try:
            content = ""
            filename = os.path.basename(file_path)
            print(f"INFO: Loading file content from: {file_path}")

            if filename.lower().endswith(".srt"):
                # Parse SRT file
                content = self._parse_srt(file_path)
                status_msg = f"✅ Loaded dialogue from {filename}" if content else f"⚠️ No dialogue found in SRT: {filename}"
            else:
                # Read plain text file
                try:
                    # Try UTF-8 encoding first (more common)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                except UnicodeDecodeError:
                    # If UTF-8 fails, try default system encoding (less reliable)
                    print(f"WARN: UTF-8 decoding failed for {filename}. Trying default system encoding.")
                    try:
                        # Python 3: encoding=None uses default locale encoding
                        with open(file_path, 'r', encoding=None) as f:
                            content = f.read()
                    except UnicodeDecodeError as e_enc:
                        # Handle specific encoding errors on second attempt
                        print(f"ERROR: Failed to read {filename} with default encoding: {e_enc}")
                        self.update_status(f"❌ Error reading file (encoding issue)"); return
                    except Exception as e_read_alt:
                         print(f"ERROR: Failed to read {filename} with default encoding: {e_read_alt}")
                         self.update_status(f"❌ Error reading file"); return
                except Exception as e_read_main:
                     # Catch other file reading errors (e.g., permission)
                     print(f"ERROR: Failed to read file {filename}: {e_read_main}")
                     self.update_status(f"❌ Error reading file"); return
                status_msg = f"✅ Loaded text from {filename}"

            # Insert content into the textbox
            if hasattr(self, 'textbox') and self.textbox.winfo_exists():
                 self.textbox_placeholder_active = False # Ensure placeholder is off
                 self.textbox.delete("1.0", ctk.END) # Clear old text
                 if self.default_textbox_color: # Ensure we have a valid color
                     self.textbox.configure(text_color=self.default_textbox_color) # Set normal color
                 if content:
                     self.textbox.insert("1.0", content) # Insert new text
                 # Check immediately if the loaded content was empty, and reset placeholder if so
                 self._check_and_set_placeholder()
            self.update_status(status_msg) # Update status bar
            self.set_ui_state('idle') # Update button states based on new text content

        except FileNotFoundError:
            print(f"ERROR: File not found: {file_path}")
            self.update_status("❌ Error: File not found.")
        except Exception as e:
            # Catch other errors during loading/parsing
            print(f"ERROR: Exception loading/processing file {file_path}: {e}")
            self.update_status(f"❌ Error loading file.")


    def _parse_srt(self, file_path: str) -> str:
        """Reads an SRT file and extracts only the dialogue text."""
        dialogue_lines: list[str] = []
        full_content: str = ""
        try:
            # Try reading as UTF-8 with error handling
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                full_content = f.read()
        except Exception as e: # Catch potential errors even opening the file
            print(f"ERROR: Failed to read SRT file {file_path}: {e}")
            return ""

        lines = full_content.splitlines()
        buffer: list[str] = []
        is_dialogue_block: bool = False
        # Regex patterns for line detection (more robust)
        block_number_pattern = re.compile(r'^\d+\s*$') # Matches lines with only numbers
        timestamp_pattern = re.compile(r'^\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{1,2}:\d{2}:\d{2}[,.]\d{3}.*') # Allow comma or period for ms

        for line in lines:
            line = line.strip()
            if not line:
                 # Empty line, potential end of block
                 if buffer:
                     dialogue_lines.append(" ".join(buffer))
                     buffer = []
                 is_dialogue_block = False # Reset state
            elif block_number_pattern.match(line) and not is_dialogue_block:
                 # Block number line (ignore if already in dialogue or buffer has text)
                 # Reset just in case of malformed SRT
                 if buffer:
                     dialogue_lines.append(" ".join(buffer))
                     buffer = []
                 is_dialogue_block = False
            elif timestamp_pattern.match(line):
                 # Timestamp line, definitely start of a new dialogue block
                 if buffer: # Append previous buffer if any
                     dialogue_lines.append(" ".join(buffer))
                 buffer = [] # Clear buffer for new dialogue
                 is_dialogue_block = True
            elif is_dialogue_block:
                 # Dialogue text line, clean simple tags and add to buffer
                 cleaned_line = re.sub(r'<[^>]+>', '', line) # Remove simple HTML/XML tags
                 cleaned_line = re.sub(r'{[^}]+}', '', cleaned_line) # Remove simple curly brace tags (e.g., {\an8})
                 # More aggressive cleaning (optional): remove lines starting with typical non-dialogue chars
                 # if not cleaned_line.startswith(('-', '[', '(')):
                 if cleaned_line: # Only add if not empty after cleaning
                     buffer.append(cleaned_line)
            # else: Ignore lines that don't fit patterns (comments, etc.)


        # Add the last buffer if file doesn't end with an empty line
        if buffer: dialogue_lines.append(" ".join(buffer))

        # Join all collected dialogue lines into a single string with spaces
        final_text = " ".join(line for line in dialogue_lines if line)
        # Further cleanup: Replace multiple spaces with single space
        final_text = re.sub(r'\s{2,}', ' ', final_text).strip()
        return final_text


    def save_audio(self):
        """Opens a dialog to save the temporary audio file to a user-chosen location."""
        if not self.audio_file_path or not os.path.exists(self.audio_file_path):
             self.update_status("❌ No generated audio file to save."); return
        # Ensure playback is stopped before saving
        if self.just_playback_initialized and self.player and (self.player.playing or self.player.paused):
            self.update_status("⚠️ Please stop playback before saving."); return

        try: # Create default filename from the beginning of the text
            # Use get_input_text to avoid using placeholder as filename basis
            initial_text = self.get_input_text()[:40].strip().replace("\n", " ") # Limit length
            # Sanitize filename: allow alphanumeric, space, underscore, hyphen
            sanitized_text = "".join(c for c in initial_text if c.isalnum() or c in (' ', '_', '-')).rstrip()
            # Replace spaces with underscores for better compatibility
            sanitized_text = sanitized_text.replace(' ', '_')
            # Limit length again after sanitization
            initial_filename = f"{sanitized_text[:30] if sanitized_text else 'speech'}.mp3"
        except Exception as e:
            print(f"WARN: Error generating initial filename: {e}")
            initial_filename = "speech.mp3" # Fallback name

        # Open 'Save As' dialog
        file_path = filedialog.asksaveasfilename(
            defaultextension=".mp3",
            filetypes=[("MP3 audio file", "*.mp3"), ("All files", "*.*")], # File type options
            title="Save Audio As...", # Dialog title
            initialfile=initial_filename # Default filename suggestion
        )

        if file_path: # If the user selected a path and name
            try:
                print(f"INFO: Copying temp file {self.audio_file_path} to {file_path}")
                # Copy the temporary file to the chosen destination (binary mode)
                with open(self.audio_file_path, 'rb') as src, open(file_path, 'wb') as dst:
                    # Read and write in chunks for potentially large files
                    while True:
                        chunk = src.read(8192) # Read 8KB at a time
                        if not chunk: break # End of file
                        dst.write(chunk)
                self.update_status(f"✅ Audio saved successfully to {os.path.basename(file_path)}")
            except IOError as e:
                print(f"ERROR: IOError during file save: {e}")
                self.update_status(f"❌ Error saving file: {e}")
            except Exception as e:
                print(f"ERROR: Unexpected exception during file save: {e}")
                self.update_status(f"❌ An unexpected error occurred during saving: {e}")
        else:
            # User cancelled the save dialog
            self.update_status("Save operation cancelled.")

    # --- Cleanup ---
    def _delete_temp_audio_file(self):
        """Deletes the temporary audio file if it exists, with retries."""
        path_to_delete = self.audio_file_path
        if path_to_delete and os.path.exists(path_to_delete):
            print(f"INFO: Preparing to delete temp file: {path_to_delete}")
            # Clear internal references *before* attempting deletion
            self.audio_file_path = None
            self.audio_duration = 0

            # Stop the player if it's active and loaded this file
            if self.just_playback_initialized and self.player and (self.player.playing or self.player.paused):
                 # Check if the player's source matches the file to delete
                 # Note: just_playback doesn't directly expose the loaded file path easily.
                 # We assume if a file exists, the player *might* be using it.
                 print(f"INFO: Stopping player before attempting to delete potential source file.")
                 try:
                     self.player.stop()
                     time.sleep(0.15) # Give OS time to release handle
                 except Exception as e:
                     # Ignore errors if player is already stopped or invalid
                     if "Playback has not been initialized" not in str(e):
                          print(f"WARN: Exception while stopping player before delete: {e}")

            # Attempt to delete the physical file with retries
            max_retries = 4
            retry_delay = 0.25 # seconds
            for attempt in range(max_retries):
                try:
                    os.remove(path_to_delete)
                    print(f"INFO: Deleted temp file: {path_to_delete}")
                    path_to_delete = None # Mark as deleted successfully
                    break # Exit loop if successful
                except PermissionError as e:
                    print(f"WARN: Attempt {attempt + 1}/{max_retries} - PermissionError removing temp file {path_to_delete}: {e}. Retrying...")
                    time.sleep(retry_delay)
                except OSError as e:
                    print(f"ERROR: Attempt {attempt + 1}/{max_retries} - OSError removing temp file {path_to_delete}: {e}. Retrying...")
                    time.sleep(retry_delay)
                except Exception as e:
                    print(f"ERROR: Unexpected exception deleting temp file {path_to_delete} (Attempt {attempt + 1}): {e}")
                    break # Don't retry on unexpected errors

            if path_to_delete and os.path.exists(path_to_delete):
                print(f"ERROR: Failed to delete temp file after {max_retries} retries: {path_to_delete}")
                # Log the failure, internal path is already None.
        else:
            # Path was already None or file didn't exist
            if self.audio_file_path: # Clear internal refs just in case
                self.audio_file_path = None
                self.audio_duration = 0

    def store_ui_state(self):
        """Saves the current audio settings to a file."""
        voice = self.voice_dropdown.get()
        if (not voice
                or voice == "Select Voice"
                or "Loading" in voice
                or "No match" in voice
                or voice == "No voices found"):
            voice = None

        settings = StoredUiState(int(self.rate_slider.get()), int(self.pitch_slider.get()), voice)
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(settings.__dict__, f)
            print(f"INFO: Audio settings saved to {CONFIG_PATH}")
        except Exception as e:
            print(f"ERROR: Failed to save audio settings: {e}")

    def on_closing(self):
        self.store_ui_state()

        """Called when the application window is closed."""
        print("INFO: Closing application...")
        self._stop_progress_updater() # Stop the UI update loop

        # Stop the player if active
        if self.just_playback_initialized and self.player:
             try:
                  if self.player.playing or self.player.paused:
                       print("INFO: Stopping audio playback...")
                       self.player.stop()
                       time.sleep(0.1) # Short pause after stopping
             except Exception as e:
                  # Ignore "Playback has not been initialized" error if player failed
                  if "Playback has not been initialized" not in str(e):
                     print(f"WARN: Exception stopping player during close: {e}")

        # Delete the last temporary file
        print("INFO: Cleaning up temporary audio file...")
        self._delete_temp_audio_file()

        # I Removed the problematic after_cancel loop entirely
        # The _stop_progress_updater() call above already handles the main updater
        # Got exceptions during the after_cancel call otherwise.

        self.destroy() # Close the Tkinter window


# --- Execution Entry Point ---
if __name__ == "__main__":
    # Check if just_playback is available before starting the main GUI
    if not JUST_PLAYBACK_AVAILABLE:
        # Display a simple error window if the library is missing
        error_root = ctk.CTk()
        # Set mode for the error window too
        ctk.set_appearance_mode("System")
        error_root.title("Dependency Error")
        error_root.geometry("450x120")
        error_label = ctk.CTkLabel(
            error_root,
            text="Required library 'just_playback' is missing or failed to load.\n"
                 "Please install it using:\n"
                 "pip install just_playback",
            font=ctk.CTkFont(size=13)
        )
        error_label.pack(pady=20, padx=20)
        # Automatically close the error window after a few seconds
        error_root.after(7000, error_root.destroy)
        error_root.mainloop()
    else:
        # If the library is available, run the main application
        app = EdgeTTSApp()
        # Set the close window action to call our on_closing method
        app.protocol("WM_DELETE_WINDOW", app.on_closing)
        app.mainloop()

# --- END OF FILE final.py ---