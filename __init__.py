bl_info = {
    "name": "Multi-Audio Track/Channel Importer",
    "author": "Parham Ettehadieh, tintwotin (& Modified by AI)",
    "version": (1, 17), # No code change needed for pan values, logic was correct
    "blender": (3, 0, 0),
    "location": "Video Sequence Editor > Sidebar > Multi-Audio",
    "description": "Select audio stream, import channels as separate panned strips (based on Scene Audio setting), or downmix.",
    "category": "Sequencer",
    "warning": "This add-on uses FFmpeg/FFprobe (must be installed and in PATH) and was created with the help of AI.",
    "doc_url": "https://www.youtube.com/@macarthurz",
}

import bpy
import subprocess
import os
import tempfile
import json
import shutil
import re # For parsing channel layout simply

from bpy.props import StringProperty, CollectionProperty, BoolProperty, IntProperty, EnumProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup, UIList

# --- FFmpeg/FFprobe Path Helper ---
def find_executable(executable_name):
    path = shutil.which(executable_name)
    if path:
        if os.name == 'nt' and not path.lower().endswith(".exe"):
             alt_path = path + ".exe";
             if shutil.which(alt_path): return alt_path
        return path
    print(f"Warning: Could not find '{executable_name}' in system PATH.")
    return None

FFPROBE_PATH = find_executable("ffprobe")
FFMPEG_PATH = find_executable("ffmpeg")

# --- Define Pan Preset Items (Only for Mono Downmix) ---
pan_preset_items = [
    ('FRONTLEFT',   'Front Left',   'Pan Front Left'),
    ('FRONTCENTER', 'Front Center', 'Pan Center'),
    ('FRONTRIGHT',  'Front Right',  'Pan Front Right'),
    ('SIDELEFT',    'Side Left',    'Pan Side Left'),
    ('SIDERIGHT',   'Side Right',   'Pan Side Right'),
    ('REARLEFT',    'Rear Left',    'Pan Rear Left'),
    ('REARRIGHT',   'Rear Right',   'Pan Rear Right'),
]

# --- Channel Layout Mapping ---
# Maps common ffprobe layouts to ffmpeg filter layouts and standard channel names
CHANNEL_LAYOUT_MAP = {
    "mono": {"ffmpeg_layout": "mono", "channels": ["FC"]},
    "stereo": {"ffmpeg_layout": "stereo", "channels": ["FL", "FR"]},
    "2.1": {"ffmpeg_layout": "2.1", "channels": ["FL", "FR", "LFE"]},
    "3.0": {"ffmpeg_layout": "3.0", "channels": ["FL", "FR", "FC"]},
    "3.0(back)": {"ffmpeg_layout": "3.0(back)", "channels": ["FL", "FR", "BC"]},
    "3.1": {"ffmpeg_layout": "3.1", "channels": ["FL", "FR", "FC", "LFE"]},
    "4.0": {"ffmpeg_layout": "quad", "channels": ["FL", "FR", "BL", "BR"]}, # Assuming quad is FL,FR,BL,BR
    "quad": {"ffmpeg_layout": "quad", "channels": ["FL", "FR", "BL", "BR"]},
    "quad(side)": {"ffmpeg_layout": "quad(side)", "channels": ["FL", "FR", "SL", "SR"]},
    "4.1": {"ffmpeg_layout": "4.1", "channels": ["FL", "FR", "FC", "LFE", "BC"]},
    "5.0": {"ffmpeg_layout": "5.0(side)", "channels": ["FL", "FR", "FC", "SL", "SR"]}, # Default 5.0 to side
    "5.0(side)": {"ffmpeg_layout": "5.0(side)", "channels": ["FL", "FR", "FC", "SL", "SR"]},
    "5.1": {"ffmpeg_layout": "5.1(side)", "channels": ["FL", "FR", "FC", "LFE", "SL", "SR"]}, # Default 5.1 to side
    "5.1(side)": {"ffmpeg_layout": "5.1(side)", "channels": ["FL", "FR", "FC", "LFE", "SL", "SR"]},
    "6.0": {"ffmpeg_layout": "6.0(front)", "channels": ["FL", "FR", "FC", "BC", "SL", "SR"]},
    "6.0(front)": {"ffmpeg_layout": "6.0(front)", "channels": ["FL", "FR", "FC", "BC", "SL", "SR"]},
    "6.1": {"ffmpeg_layout": "6.1(back)", "channels": ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"]},
    "6.1(back)": {"ffmpeg_layout": "6.1(back)", "channels": ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"]},
    "6.1(front)": {"ffmpeg_layout": "6.1(front)", "channels": ["FL", "FR", "FC", "LFE", "SL", "SR"]}, # Verify this mapping if needed
    "7.0": {"ffmpeg_layout": "7.0(front)", "channels": ["FL", "FR", "FC", "BL", "BR", "SL", "SR"]},
    "7.0(front)": {"ffmpeg_layout": "7.0(front)", "channels": ["FL", "FR", "FC", "BL", "BR", "SL", "SR"]},
    "7.1": {"ffmpeg_layout": "7.1", "channels": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"]},
    "7.1(wide)": {"ffmpeg_layout": "7.1(wide)", "channels": ["FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC"]},
    "7.1(wide-side)": {"ffmpeg_layout": "7.1(wide-side)", "channels": ["FL", "FR", "FC", "LFE", "SL", "SR", "FLC", "FRC"]},
    "octagonal": {"ffmpeg_layout": "octagonal", "channels": ["FL", "FR", "FC", "BL", "BR", "SL", "SR", "BC"]},
}

# Maps FFmpeg standard channel names (uppercase) to the Pan Preset Keys (uppercase)
CHANNEL_NAME_TO_PAN_KEY = {
    "FL": "FRONTLEFT", "FR": "FRONTRIGHT", "FC": "FRONTCENTER", "LFE": "FRONTCENTER",
    "BL": "REARLEFT", "BR": "REARRIGHT", "FLC": "FRONTLEFT", "FRC": "FRONTRIGHT",
    "BC": "FRONTCENTER", "SL": "SIDELEFT", "SR": "SIDERIGHT", "TC": "FRONTCENTER",
    "TFL": "FRONTLEFT", "TFC": "FRONTCENTER", "TFR": "FRONTRIGHT", "TBL": "REARLEFT",
    "TBC": "FRONTCENTER", "TBR": "REARRIGHT",
    # Generic fallbacks (less accurate panning but better than nothing)
    "Ch1": "FRONTLEFT", "Ch2": "FRONTRIGHT", "Ch3": "FRONTCENTER", "Ch4": "REARLEFT",
    "Ch5": "REARRIGHT", "Ch6": "SIDELEFT", "Ch7": "SIDERIGHT", "Ch8": "FRONTCENTER",
}

# --- Helper: Get Audio Streams ---
def get_audio_streams_info(media_path):
    if not FFPROBE_PATH: print("Error: ffprobe not found."); return None
    try:
        cmd = [ FFPROBE_PATH, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index,codec_name,sample_rate,channels,channel_layout:stream_tags=language,title", "-of", "json", media_path ]
        print(f"Running ffprobe (get streams): {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8')
        if not result.stdout.strip(): print("ffprobe: No audio streams."); return []
        try: data = json.loads(result.stdout)
        except json.JSONDecodeError as e: print(f"JSON Error: {e}\nOut:{result.stdout}"); return None
        streams = data.get("streams", [])
        valid_streams = [s for s in streams if s.get("index") is not None]
        if not valid_streams: print("ffprobe: No streams with index found.")
        for i, stream in enumerate(valid_streams): stream['relative_audio_index'] = i
        return valid_streams
    except subprocess.CalledProcessError as e: print(f"ffprobe Error {e.returncode}: {e.stderr.strip()}"); return None
    except Exception as e: print(f"ffprobe Exception: {e}"); return None

# --- Helper: Has Video Stream ---
def has_video_stream(media_path):
    if not FFPROBE_PATH: return False
    try:
        cmd = [ FFPROBE_PATH, "-v", "error", "-select_streams", "v", "-show_entries", "stream=index", "-of", "csv=p=0", media_path ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, encoding='utf-8')
        return bool(result.stdout.strip())
    except Exception as e: print(f"Video Check Warn: {e}"); return False

# --- Property Group Definitions ---
class AudioStreamItem(PropertyGroup):
    index: IntProperty(name="Stream Index")
    relative_audio_index: IntProperty(name="Relative Index")
    codec_name: StringProperty(name="Codec")
    sample_rate: IntProperty(name="Sample Rate")
    channels: IntProperty(name="Channels", default=0)
    channel_layout: StringProperty(name="Channel Layout", default="")
    language: StringProperty(name="Language")
    title: StringProperty(name="Title")

class AudioChannelItem(PropertyGroup):
    name: StringProperty(name="Channel Name")
    index: IntProperty(name="Channel Index")
    selected: BoolProperty(name="Import", default=True)

# --- UI List Definitions ---
class STREAM_UL_List(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        props = context.scene.multi_audio_props
        layout.active = (props.stream_index == index)
        row = layout.row(align=True)
        row.label(text=f"Stream {item.index} ({item.relative_audio_index})")
        ch_text = f"{item.channels}ch" if item.channels > 0 else "N/A ch"
        if item.channel_layout: ch_text += f" ({item.channel_layout})"
        row.label(text=ch_text)
        label_text = item.language if item.language and item.language.lower() != 'und' else ""
        if item.title: label_text += f" ({item.title})" if label_text else item.title
        if not label_text: label_text = item.codec_name or "Unknown"
        row.label(text=label_text, translate=False)

class CHANNEL_UL_List(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=f"Channel {item.index}: {item.name}")

# --- Main Properties Container ---
class MultiAudioProperties(PropertyGroup):
    media_path: StringProperty(name="Media File", subtype='FILE_PATH', update=lambda s,c: MultiAudioProperties.path_updated(s,c))
    make_mono: BoolProperty( name="Downmix Selected Stream to Mono", description="CHECKED: Downmix selected stream to mono (uses Pan Preset below).\nUNCHECKED: Import selected channels below as separate, auto-panned mono strips.", default=False, update=lambda s,c: MultiAudioProperties.options_updated(s,c) )
    pack_audio: BoolProperty( name="Pack Audio Data", description="Embed extracted audio into .blend file? If unchecked, links to temporary file.", default=False )
    pan_preset: EnumProperty(items=pan_preset_items, name="Pan Preset", description="Pan preset (only used if 'Downmix to Mono' is checked)", default='FRONTCENTER')
    streams: CollectionProperty(type=AudioStreamItem, name="Detected Audio Streams")
    stream_index: IntProperty(name="Selected Stream Index", update=lambda s,c: MultiAudioProperties.stream_selected(s,c), default=-1)
    channels: CollectionProperty(type=AudioChannelItem, name="Channels in Selected Stream")
    channel_index: IntProperty(name="Selected Channel Index")
    scan_attempted: BoolProperty(default=False)

    @staticmethod
    def path_updated(self, context):
        print("Path updated, clearing.")
        self.streams.clear(); self.stream_index = -1
        self.channels.clear(); self.channel_index = 0
        self.scan_attempted = False

    @staticmethod
    def stream_selected(self, context):
        print(f"Stream selection changed to index: {self.stream_index}")
        self.channels.clear(); self.channel_index = 0
        if not (0 <= self.stream_index < len(self.streams)): print(" Invalid index."); return
        stream = self.streams[self.stream_index]
        n_ch = stream.channels; layout = stream.channel_layout
        print(f" Populating channels for stream {stream.index} ({n_ch} channels, layout '{layout}')")
        l_info = CHANNEL_LAYOUT_MAP.get(layout)
        chnames = []
        if l_info and len(l_info["channels"]) == n_ch: chnames = l_info["channels"]
        else:
            if layout and not l_info: print(f" Warn: Layout '{layout}' not mapped.")
            elif l_info: print(f" Warn: Map/detect channel mismatch ({len(l_info['channels'])} vs {n_ch}).")
            chnames = [f"Ch{i+1}" for i in range(n_ch)]; print(f" Using generic names: {chnames}")
        for i in range(n_ch):
            item = self.channels.add(); item.name = chnames[i]; item.index = i; item.selected = True

    @staticmethod
    def options_updated(self, context): pass # For future use if UI needs dynamic updates

# --- UI panel ---
class SEQUENCER_PT_MultiAudioImport(Panel):
    bl_label = "Multi-Audio Import"; bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'; bl_category = 'Multi-Audio'
    def draw_header(self, context): self.layout.label(text="", icon='SOUND')
    def draw(self, context):
        layout = self.layout; props = context.scene.multi_audio_props
        abspath = bpy.path.abspath(props.media_path); path_exists = os.path.exists(abspath)
        col = layout.column()
        col.label(text="Input Media File:"); col.prop(props, "media_path", text="")
        scan_row = col.row(align=True)
        scan_row.operator("multi_audio.scan_tracks", icon="FILE_REFRESH", text="Scan Media File")
        scan_row.enabled = props.media_path != "" and path_exists
        info_row = col.row(align=True)
        info_row.operator("info.show_media_info", icon='INFO', text="Show Full Media Info")
        info_row.enabled = props.media_path != "" and path_exists

        if props.streams:
            col.separator(); col.label(text="1. Select Audio Stream:")
            col.template_list("STREAM_UL_List", "stream_list", props, "streams", props, "stream_index", rows=3)

            if props.stream_index >= 0 and props.stream_index < len(props.streams):
                 selected_stream = props.streams[props.stream_index]
                 col.separator()
                 col.label(text=f"2. Select Channels from Stream {selected_stream.index}:")
                 col.template_list("CHANNEL_UL_List", "channel_list", props, "channels", props, "channel_index", rows=max(3, selected_stream.channels + 1))

                 col.separator(); box = col.box(); box.label(text="3. Import Options:")
                 box.prop(props, "make_mono")
                 pan_row = box.row(); pan_row.prop(props, "pan_preset"); pan_row.enabled = props.make_mono
                 box.prop(props, "pack_audio")

                 can_import_channels = not props.make_mono and any(c.selected for c in props.channels)
                 can_import = props.stream_index >= 0 and (props.make_mono or can_import_channels)
                 import_row = box.row(align=True); import_row.enabled = can_import
                 import_row.operator("multi_audio.import_media", icon='SEQ_SEQUENCER', text="Import Selected")
            else: col.label(text="Select a stream above to see channels.", icon='INFO')
        elif props.scan_attempted: col.label(text="No audio streams found.", icon='INFO')
        else:
            if props.media_path and path_exists: col.label(text="Scan file to find audio streams.", icon='INFO')
            elif props.media_path: col.label(text="Media file path invalid.", icon='ERROR')
            else: col.label(text="Select a media file.", icon='INFO')
        if not FFMPEG_PATH or not FFPROBE_PATH:
            layout.separator(); box = layout.box(); box.alert = True
            box.label(text="FFmpeg/FFprobe Not Found!", icon='ERROR')
            box.label(text="Ensure installed & in system PATH.");
            if os.name == 'nt': box.label(text="(Restart Blender?)")

# --- Operator: Scan ---
class AUDIO_OT_ScanTracks(Operator):
    bl_idname = "multi_audio.scan_tracks"; bl_label = "Scan Media File"
    bl_description = f"Scan for audio streams using {FFPROBE_PATH or 'ffprobe'}"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        props = context.scene.multi_audio_props
        return props.media_path != "" and os.path.exists(bpy.path.abspath(props.media_path))
    def execute(self, context):
        props = context.scene.multi_audio_props
        props.streams.clear(); props.stream_index = -1
        props.channels.clear(); props.channel_index = 0
        props.scan_attempted = True
        media_path_abs = bpy.path.abspath(props.media_path)
        if not os.path.isfile(media_path_abs): self.report({'ERROR'}, "Invalid file path."); return {'CANCELLED'}
        if not FFPROBE_PATH: self.report({'ERROR'}, "ffprobe not found."); return {'CANCELLED'}

        self.report({'INFO'}, f"Scanning '{os.path.basename(media_path_abs)}'...");
        wm = context.window_manager; wm.progress_begin(0, 1); wm.progress_update(0.5)
        audio_streams_data = get_audio_streams_info(media_path_abs); wm.progress_end()

        if audio_streams_data is None: self.report({'ERROR'}, "ffprobe failed. Check console."); return {'CANCELLED'}
        if not audio_streams_data: self.report({'INFO'}, "No audio streams found."); return {'FINISHED'}

        for i, stream_data in enumerate(audio_streams_data):
            item = props.streams.add()
            item.relative_audio_index = stream_data.get("relative_audio_index", i)
            item.codec_name = stream_data.get("codec_name", "N/A")
            item.channel_layout = stream_data.get("channel_layout", "")
            tags = stream_data.get("tags", {})
            item.language = tags.get("language", "")
            item.title = tags.get("title", "")
            try: item.index = int(stream_data.get("index", -1))
            except (ValueError, TypeError): item.index = -1
            try: item.sample_rate = int(stream_data.get("sample_rate", 0))
            except (ValueError, TypeError): item.sample_rate = 0
            try: item.channels = int(stream_data.get("channels", 0))
            except (ValueError, TypeError): item.channels = 0
            if item.index == -1: print(f"WARN: Failed to parse index for stream {i}")

        if len(props.streams) > 0: props.stream_index = 0
        else: props.stream_index = -1
        self.report({'INFO'}, f"Found {len(props.streams)} audio stream(s). Select one."); return {'FINISHED'}

# --- Operator: Show Media Info ---
class INFO_OT_ShowMediaInfo(Operator):
    bl_idname = "info.show_media_info"; bl_label = "Show Full Media Info"
    bl_description = "Display detailed media info via ffprobe (output to Info window and Console)"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        props = context.scene.multi_audio_props
        return props.media_path != "" and os.path.exists(bpy.path.abspath(props.media_path))
    def execute(self, context):
        props = context.scene.multi_audio_props; media_path_abs = bpy.path.abspath(props.media_path)
        if not FFPROBE_PATH: self.report({'ERROR'}, "ffprobe not found."); return {'CANCELLED'}
        cmd = [ FFPROBE_PATH, "-v", "quiet", "-show_format", "-show_streams", media_path_abs ]
        print(f"\n--- Running MediaInfo Cmd ---\n{' '.join(cmd)}\n")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8', errors='replace')
            output = result.stdout; print("--- Full MediaInfo Output ---"); print(output); print("--- End MediaInfo Output ---\n")
            self.report({'INFO'}, f"Full MediaInfo printed to System Console.")
            lines = output.splitlines()[:20]; # Limit info lines
            for line in lines: self.report({'INFO'}, line)
        except subprocess.CalledProcessError as e: print(f"ERROR: ffprobe info failed (code {e.returncode}): {e.stderr}"); self.report({'ERROR'}, f"Failed media info (Code:{e.returncode}). Console."); return {'CANCELLED'}
        except Exception as e: print(f"ERROR: Unexpected media info error: {e}"); self.report({'ERROR'}, f"Error getting media info: {e}"); return {'CANCELLED'}
        return {'FINISHED'}

# --- Operator: Import ---
class AUDIO_OT_ImportMedia(Operator):
    bl_idname = "multi_audio.import_media"; bl_label = "Import Selected"
    bl_description = "Imports selected stream (downmixed) or selected channels (split)"
    bl_options = {'REGISTER', 'UNDO'}

    # Dictionary holding the exact pan values based on scene setting name
    pan_values = {
        'STEREO':    {'FRONTLEFT': -1.0,    'FRONTCENTER': 0.0,     'FRONTRIGHT': 1.0,     'SIDELEFT': -1.0,   'SIDERIGHT': 1.0,    'REARLEFT': -1.0,    'REARRIGHT': 1.0   },
        'SURROUND4': {'FRONTLEFT': -0.5,    'FRONTCENTER': 0.0,     'FRONTRIGHT': 0.5,     'SIDELEFT': -1.5,   'SIDERIGHT': 1.5,    'REARLEFT': -1.5,    'REARRIGHT': 1.5   },
        'SURROUND51':{'FRONTLEFT': -0.33335,'FRONTCENTER': 0.0,     'FRONTRIGHT': 0.33335, 'SIDELEFT': -1.2225,'SIDERIGHT': 1.2225, 'REARLEFT': -1.2225, 'REARRIGHT': 1.2225},
        'SURROUND71':{'FRONTLEFT': -0.33335,'FRONTCENTER': 0.0,     'FRONTRIGHT': 0.33335, 'SIDELEFT': -1.2225,'SIDERIGHT': 1.2225, 'REARLEFT': -1.66667,'REARRIGHT': 1.66667},
        'MONO':      {'FRONTLEFT': 0.0,     'FRONTCENTER': 0.0,     'FRONTRIGHT': 0.0,     'SIDELEFT': 0.0,    'SIDERIGHT': 0.0,    'REARLEFT': 0.0,     'REARRIGHT': 0.0   }
    }

    def get_pan_value(self, pan_key, scene_channels_setting):
        """Looks up the precise pan value."""
        # Default to STEREO map if scene setting is unrecognized
        channel_map = self.pan_values.get(scene_channels_setting, self.pan_values['STEREO'])
        # Default to FRONTCENTER (0.0) if the specific pan_key is not in the map
        return channel_map.get(pan_key, 0.0)

    @classmethod
    def poll(cls, context):
        props = context.scene.multi_audio_props
        stream_selected = props.stream_index >= 0 and props.stream_index < len(props.streams)
        if not stream_selected: return False
        can_import_channels = not props.make_mono and any(c.selected for c in props.channels)
        return props.media_path != "" and os.path.exists(bpy.path.abspath(props.media_path)) and (props.make_mono or can_import_channels)

    def execute(self, context):
        props = context.scene.multi_audio_props; media_path_abs = bpy.path.abspath(props.media_path)
        make_mono_downmix = props.make_mono; pack_audio_data = props.pack_audio
        scene = context.scene
        if not os.path.isfile(media_path_abs): self.report({'ERROR'}, "Media file invalid."); return {'CANCELLED'}
        if not FFMPEG_PATH: self.report({'ERROR'}, "ffmpeg not found."); return {'CANCELLED'}

        if not (0 <= props.stream_index < len(props.streams)): self.report({'ERROR'}, "No valid stream selected."); return {'CANCELLED'}
        selected_stream_item = props.streams[props.stream_index]
        relative_audio_idx = selected_stream_item.relative_audio_index; abs_stream_idx = selected_stream_item.index
        stream_channels = selected_stream_item.channels; stream_layout_str = selected_stream_item.channel_layout

        if not scene.sequence_editor:
            try: scene.sequence_editor_create()
            except Exception as e: self.report({'ERROR'}, f"VSE Create fail: {e}"); return {'CANCELLED'}
        sed = scene.sequence_editor

        start_channel = 1
        if sed.sequences_all: start_channel = max(s.channel for s in sed.sequences_all) + 1

        has_video = has_video_stream(media_path_abs); video_strip = None
        video_channel = start_channel; current_channel_offset = 0

        # --- Import Video ---
        if has_video:
            video_name = os.path.basename(media_path_abs)
            try:
                video_strip = sed.sequences.new_movie( name=video_name[:63], filepath=media_path_abs, channel=video_channel, frame_start=scene.frame_current )
                if video_strip: print(f"Added video strip: {video_strip.name} on Ch {video_channel}"); current_channel_offset = 1
                else: print("ERROR: new_movie fail!"); current_channel_offset = 1
            except Exception as e: self.report({'ERROR'}, f"Video Add Fail: {e}")

        # --- Import Audio ---
        imported_strips_list = []; temp_files_this_op = []
        scene_audio_channels = scene.render.ffmpeg.audio_channels # Get scene setting for panning

        # === CASE 1: SPLIT CHANNELS ===
        if not make_mono_downmix and stream_channels > 1:
            selected_channels = [ch for ch in props.channels if ch.selected]
            if not selected_channels: self.report({'ERROR'}, "No channels selected."); return {'CANCELLED'}
            print(f"\nSplitting Stream {abs_stream_idx}: {[ch.name for ch in selected_channels]}")
            layout_info = CHANNEL_LAYOUT_MAP.get(stream_layout_str)
            if not layout_info: # Basic layout guess
                if stream_channels == 2: layout_info = CHANNEL_LAYOUT_MAP.get("stereo")
                elif stream_channels > 2: layout_info = {"ffmpeg_layout": f"{stream_channels}.0", "channels": [f"Ch{i+1}" for i in range(stream_channels)]}
            if not layout_info: self.report({'ERROR'}, f"Cannot map layout '{stream_layout_str}'."); return {'CANCELLED'}
            ffmpeg_layout = layout_info["ffmpeg_layout"]; all_chnames = layout_info["channels"]
            if len(all_chnames) != stream_channels: self.report({'ERROR'}, f"Layout map mismatch: {stream_channels} vs {len(all_chnames)}."); return {'CANCELLED'}

            filter_complex = f"[0:a:{relative_audio_idx}]channelsplit=channel_layout={ffmpeg_layout}"
            map_args = []; temp_file_map = {}
            try:
                for ch_item in selected_channels:
                    ch_name = ch_item.name; filter_complex += f"[{ch_name}]"
                    temp_fd, temp_path = tempfile.mkstemp(prefix=f"bimport_s{abs_stream_idx}_ch_{ch_name}_", suffix=".wav")
                    os.close(temp_fd); temp_files_this_op.append({"path": temp_path, "pack": pack_audio_data})
                    temp_file_map[ch_name] = temp_path; map_args.extend(["-map", f"[{ch_name}]", temp_path])
            except Exception as e: self.report({'ERROR'}, f"Temp file create fail: {e}"); return {'CANCELLED'}
            if not map_args: self.report({'ERROR'}, "No channels mapped."); return {'CANCELLED'}

            ffmpeg_cmd = [ FFMPEG_PATH, "-y", "-i", media_path_abs, "-vn", "-filter_complex", filter_complex ] + map_args
            print(f"  Split FFmpeg: {' '.join(ffmpeg_cmd)}")
            try:
                result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True, encoding='utf-8', timeout=300)
                audio_base_channel = start_channel + current_channel_offset; strips_added_count = 0
                for i_ch, ch_item in enumerate(selected_channels):
                     ch_name = ch_item.name; ch_temp_path = temp_file_map.get(ch_name);
                     if not ch_temp_path: continue
                     ch_vse_channel = audio_base_channel + i_ch; strip_name = f"Str_{abs_stream_idx}_{ch_name}"
                     try:
                         new_strip = sed.sequences.new_sound( name=strip_name[:63], filepath=ch_temp_path, channel=ch_vse_channel, frame_start=scene.frame_current )
                         if new_strip:
                             imported_strips_list.append(new_strip); sound_db = new_strip.sound
                             if sound_db:
                                 sound_db.use_mono = True # Split channels are always mono
                                 if pack_audio_data:
                                      try: sound_db.pack()
                                      except RuntimeError as e: self.report({'WARNING'}, f"Pack fail {new_strip.name}: {e}")
                             # --- Apply Auto Pan using Scene Setting ---
                             pan_key = CHANNEL_NAME_TO_PAN_KEY.get(ch_name, "FRONTCENTER")
                             pan_val = self.get_pan_value(pan_key, scene_audio_channels) # Pass scene setting
                             new_strip.pan = pan_val
                             # --- End Auto Pan ---
                             print(f"    Added Ch Strip '{new_strip.name}' (Ch:{ch_vse_channel}), Panned {pan_key} ({pan_val:.4f}) for Scene '{scene_audio_channels}'")
                             strips_added_count += 1
                         else: self.report({'ERROR'}, f"API Fail Ch {ch_name} Str {abs_stream_idx}."); continue
                     except Exception as e: self.report({'ERROR'}, f"Add Strip Error Ch {ch_name} Str {abs_stream_idx}: {e}"); continue
                current_channel_offset += strips_added_count
            except subprocess.TimeoutExpired: self.report({'ERROR'}, f"FFmpeg split timed out Str {abs_stream_idx}."); return {'CANCELLED'}
            except subprocess.CalledProcessError as e: print(f"FFmpeg Split Err Str {abs_stream_idx}: {e.stderr.strip()}"); self.report({'ERROR'}, f"FFmpeg split failed Str {abs_stream_idx}. Console."); return {'CANCELLED'}
            except Exception as e: self.report({'ERROR'}, f"Unexpected split error Str {abs_stream_idx}: {e}"); return {'CANCELLED'}

        # === CASE 2: DOWNMIX or ORIGINAL MONO ===
        elif make_mono_downmix or stream_channels == 1:
            mode = "Downmix" if make_mono_downmix else "Original Mono"
            print(f"\nProcessing Stream {abs_stream_idx} as {mode}...")
            temp_path = ""
            try:
                temp_fd, temp_path = tempfile.mkstemp(prefix=f"bimport_s{abs_stream_idx}_{mode.lower().replace(' ','')}_", suffix=".wav")
                os.close(temp_fd); temp_files_this_op.append({"path": temp_path, "pack": pack_audio_data})
                ffmpeg_cmd = [ FFMPEG_PATH, "-y", "-i", media_path_abs, "-map", f"0:a:{relative_audio_idx}", "-vn" ]
                is_mono_strip = True # Both cases result in mono strip
                if make_mono_downmix: ffmpeg_cmd.extend(["-ac", "1"]) # Force mono only if downmixing
                ffmpeg_cmd.append(temp_path)
                print(f"  Running {mode} FFmpeg: {' '.join(ffmpeg_cmd)}")
                result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True, encoding='utf-8', timeout=300)
                strip_channel = start_channel + current_channel_offset; current_channel_offset += 1
                lang = selected_stream_item.language.replace(" ", "_") if selected_stream_item.language else "Track"
                title = f"_{selected_stream_item.title.replace(' ','_')}" if selected_stream_item.title else ""
                strip_name = f"{lang}{title}_{abs_stream_idx}";
                if make_mono_downmix: strip_name += "_Mono"
                new_strip = sed.sequences.new_sound( name=strip_name[:63], filepath=temp_path, channel=strip_channel, frame_start=scene.frame_current )
                if new_strip:
                    imported_strips_list.append(new_strip); sound_db = new_strip.sound
                    if sound_db:
                        sound_db.use_mono = is_mono_strip
                        print(f"  Sound DB '{sound_db.name}': Ch={sound_db.channels}, Mono={sound_db.use_mono}")
                        if pack_audio_data:
                            try: sound_db.pack()
                            except RuntimeError as e: self.report({'WARNING'}, f"Pack fail {new_strip.name}: {e}")
                    # --- Use Pan Preset for downmix, Center for original mono ---
                    pan_key_to_use = props.pan_preset if make_mono_downmix else "FRONTCENTER"
                    pan_val = self.get_pan_value(pan_key_to_use, scene_audio_channels)
                    new_strip.pan = pan_val
                    # --- End Pan ---
                    print(f"    Added {mode} Strip '{new_strip.name}' (Ch:{strip_channel}), Panned {pan_key_to_use} ({pan_val:.4f}) for Scene '{scene_audio_channels}'")
                else: self.report({'ERROR'}, f"API Fail {mode} Str {abs_stream_idx}.")
            except subprocess.TimeoutExpired: self.report({'ERROR'}, f"FFmpeg timed out Str {abs_stream_idx}."); return {'CANCELLED'}
            except subprocess.CalledProcessError as e: print(f"FFmpeg Err Str {abs_stream_idx}: {e.stderr.strip()}"); self.report({'ERROR'}, f"FFmpeg failed Str {abs_stream_idx}. Console."); return {'CANCELLED'}
            except Exception as e: self.report({'ERROR'}, f"Unexpected {mode} strip error Str {abs_stream_idx}: {e}"); return {'CANCELLED'}

        # --- Cleanup & Final Report ---
        try:
            imported_audio_count = len(imported_strips_list)
            report_parts = []; report_level = 'INFO'
            if video_strip: report_parts.append("Video imported.")
            elif has_video: report_parts.append("Video failed import."); report_level = 'WARNING'
            if imported_audio_count > 0: report_parts.append(f"{imported_audio_count} audio strip(s) imported.")
            elif props.stream_index >=0 : report_parts.append("Selected audio FAILED."); report_level = 'ERROR'
            if not report_parts: report_parts.append("Nothing imported."); report_level = 'WARNING';
            self.report({report_level}, " ".join(report_parts))
            if imported_strips_list:
                for s in sed.sequences_all: s.select = False
                for s in imported_strips_list:
                    try: s.select = True
                    except ReferenceError: pass
                if imported_strips_list and hasattr(imported_strips_list[-1], 'name'):
                     if hasattr(sed, 'active_strip'):
                         try: sed.active_strip = imported_strips_list[-1]
                         except TypeError as e: print(f"Warn: Set active fail: {e}")
                     else: print("Warn: No active_strip.")
        finally:
            files_to_del = [f["path"] for f in temp_files_this_op if f["pack"]]
            if files_to_del:
                print(f"Cleaning up {len(files_to_del)} temp files..."); del_cnt=0; err_cnt=0
                for f in files_to_del:
                    try:
                        if os.path.exists(f): os.remove(f); del_cnt+=1
                    except OSError as e: print(f"Warn: Del fail {f}: {e}"); err_cnt+=1
                print(f"  Deleted {del_cnt}/{len(files_to_del)}.{f' ({err_cnt} err)' if err_cnt else ''}")
            elif temp_files_this_op: print("No temp files marked for deletion.")

        return {'FINISHED'}

# --- Property Container ---
class MultiAudioProperties(PropertyGroup):
    media_path: StringProperty(name="Media File", subtype='FILE_PATH', update=lambda s,c: MultiAudioProperties.path_updated(s,c))
    make_mono: BoolProperty( name="Downmix Selected Stream to Mono", description="CHECKED: Downmix selected stream to mono (uses Pan Preset below).\nUNCHECKED: Import selected channels below as separate, auto-panned mono strips.", default=False, update=lambda s,c: MultiAudioProperties.options_updated(s,c) )
    pack_audio: BoolProperty( name="Pack Audio Data", description="Embed extracted audio into .blend file? If unchecked, links to temporary file.", default=False )
    pan_preset: EnumProperty(items=pan_preset_items, name="Pan Preset", description="Pan preset (only used if 'Downmix to Mono' is checked)", default='FRONTCENTER')
    streams: CollectionProperty(type=AudioStreamItem, name="Detected Audio Streams")
    stream_index: IntProperty(name="Selected Stream Index", update=lambda s,c: MultiAudioProperties.stream_selected(s,c), default=-1)
    channels: CollectionProperty(type=AudioChannelItem, name="Channels in Selected Stream")
    channel_index: IntProperty(name="Selected Channel Index")
    scan_attempted: BoolProperty(default=False)

    @staticmethod
    def path_updated(self, context):
        print("Path updated, clearing.")
        self.streams.clear(); self.stream_index = -1
        self.channels.clear(); self.channel_index = 0
        self.scan_attempted = False

    @staticmethod
    def stream_selected(self, context):
        print(f"Stream selection changed to index: {self.stream_index}")
        self.channels.clear(); self.channel_index = 0
        if not (0 <= self.stream_index < len(self.streams)): print(" Invalid index."); return
        stream = self.streams[self.stream_index]
        n_ch = stream.channels; layout = stream.channel_layout
        print(f" Populating channels for stream {stream.index} ({n_ch} channels, layout '{layout}')")
        l_info = CHANNEL_LAYOUT_MAP.get(layout)
        chnames = []
        if l_info and len(l_info["channels"]) == n_ch: chnames = l_info["channels"]
        else:
            if layout and not l_info: print(f" Warn: Layout '{layout}' not mapped.")
            elif l_info: print(f" Warn: Map/detect channel mismatch ({len(l_info['channels'])} vs {n_ch}).")
            chnames = [f"Ch{i+1}" for i in range(n_ch)]; print(f" Using generic names: {chnames}")
        for i in range(n_ch):
            item = self.channels.add(); item.name = chnames[i]; item.index = i; item.selected = True

    @staticmethod
    def options_updated(self, context): pass # Trigger UI update if needed later

# --- Register/Unregister ---
classes = ( AudioStreamItem, AudioChannelItem, MultiAudioProperties, STREAM_UL_List, CHANNEL_UL_List, SEQUENCER_PT_MultiAudioImport, AUDIO_OT_ScanTracks, INFO_OT_ShowMediaInfo, AUDIO_OT_ImportMedia )
def register():
    if not FFPROBE_PATH: print(f"WARN [{bl_info.get('name')}]: ffprobe not found.")
    if not FFMPEG_PATH: print(f"WARN [{bl_info.get('name')}]: ffmpeg not found.")
    for cls in classes:
        try: bpy.utils.register_class(cls)
        except ValueError: pass
    try: bpy.types.Scene.multi_audio_props = bpy.props.PointerProperty(type=MultiAudioProperties)
    except Exception as e: print(f"Error setting PointerProperty: {e}")
    print(f"{bl_info.get('name')} version {bl_info.get('version')} registered.")
def unregister():
    print(f"Unregistering {bl_info.get('name')} version {bl_info.get('version')}...")
    if hasattr(bpy.types.Scene, 'multi_audio_props'):
        try:
            if 'multi_audio_props' in bpy.types.Scene.bl_rna.properties: del bpy.types.Scene.multi_audio_props
        except Exception as e: print(f"Warn: Error removing PointerProperty: {e}")
    for cls in reversed(classes):
         if hasattr(bpy.utils, "unregister_class"):
             try: bpy.utils.unregister_class(cls)
             except RuntimeError: pass
             except Exception as e: print(f"Error unregistering {cls.__name__}: {e}")
if __name__ == "__main__": register()
