import cv2
import torch
import clip
import os
from PIL import Image
from pytube import YouTube
from scenedetect import VideoManager, SceneManager
from scenedetect.detectors import ContentDetector
from moviepy.editor import VideoFileClip, concatenate_videoclips, AudioFileClip
from moviepy.video.fx import all
import logging
import base64
import numpy as np

# Load CLIP model
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load('ViT-B/32', device=device)

# Configure logging to suppress verbose output from libraries
logging.getLogger('moviepy').setLevel(logging.ERROR)
logging.getLogger('pyscenedetect').setLevel(logging.ERROR)

# Define base directory for saving files (adjust as per deployment environment)
BASE_DIRECTORY = "static/videos"

def download_video(url):
    yt = YouTube(url)
    stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
    if stream:
        return stream.download(output_path=BASE_DIRECTORY)
    else:
        return None

def image_to_base64(image):
    _, buffer = cv2.imencode('.jpg', image)
    jpg_as_text = base64.b64encode(buffer).decode('utf-8')
    return jpg_as_text



def find_scenes(video_path):
    video_manager = VideoManager([video_path])
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector())
    video_manager.set_downscale_factor()
    video_manager.start()
    scene_manager.detect_scenes(frame_source=video_manager)
    scene_list = scene_manager.get_scene_list(video_manager.get_base_timecode())
    video_manager.release()
    return scene_list

def extract_frames(video_path, scene_list):
    scene_frames = {}
    cap = cv2.VideoCapture(video_path)
    for i, (start_time, end_time) in enumerate(scene_list):
        frames = []
        first_frame = None
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_time.get_frames())
        while cap.get(cv2.CAP_PROP_POS_FRAMES) < end_time.get_frames():
            ret, frame = cap.read()
            if ret:
                if first_frame is None:
                    first_frame = frame
                frames.append(frame)
        scene_frames[i] = {'start_time': start_time, 'end_time': end_time, 'frames': frames, 'first_frame': first_frame}
        print(f"Extracted frames for scene {i}: First frame type: {type(first_frame)}, Total frames extracted: {len(frames)}")
    cap.release()
    return scene_frames

def classify_and_categorize_scenes(scene_frames, description_phrases):
    scene_categories = {}
    description_texts = description_phrases

    action_indices = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    context_indices = list(set(range(len(description_texts))) - set(action_indices))

    for scene_id, scene_data in scene_frames.items():
        frames = scene_data['frames']
        first_frame = scene_data['first_frame']

        # Debug output to verify the type of the first_frame
        if not isinstance(first_frame, np.ndarray):
            print(f"Error: First frame for scene {scene_id} is not a numpy array. Type: {type(first_frame)}")
            continue  # Skip this scene if the first frame is not a numpy array

        scene_scores = [0] * len(description_texts)
        valid_frames = 0

        for frame in frames:
            try:
                image = Image.fromarray(frame[..., ::-1])
                image_input = preprocess(image).unsqueeze(0).to(device)
                with torch.no_grad():
                    text_inputs = clip.tokenize(description_texts).to(device)
                    text_features = model.encode_text(text_inputs)
                    image_features = model.encode_image(image_input)
                    logits = (image_features @ text_features.T).squeeze()
                    probs = logits.softmax(dim=0)
                    scene_scores = [sum(x) for x in zip(scene_scores, probs.tolist())]
                    valid_frames += 1
            except Exception as e:
                print(f"An error occurred while processing frame: {e}")
                continue

        if valid_frames > 0:
            scene_scores = [score / valid_frames for score in scene_scores]
            action_confidence = sum(scene_scores[i] for i in action_indices) / len(action_indices)
            context_confidence = sum(scene_scores[i] for i in context_indices) / len(context_indices)

            best_description_index = scene_scores.index(max(scene_scores))
            best_description = description_texts[best_description_index]

            if action_confidence > context_confidence:
                category = "Action Scene"
                confidence = action_confidence
            else:
                category = "Context Scene"
                confidence = context_confidence

            duration = scene_data['end_time'].get_seconds() - scene_data['start_time'].get_seconds()
            scene_categories[scene_id] = {
                "category": category,
                "confidence": confidence,
                "start_time": str(scene_data['start_time']),
                "end_time": str(scene_data['end_time']),
                "duration": duration,
                "first_frame": first_frame,  # Assuming first_frame is already handled as numpy array
                "best_description": best_description
            }

    return scene_categories

def add_text_with_opencv(frame, text, font_scale=2.0, font=cv2.FONT_HERSHEY_COMPLEX, color=(255, 255, 0), thickness=3):
    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    text_size, _ = cv2.getTextSize(text, font, font_scale, thickness)
    text_width, text_height = text_size

    text_x = (frame.shape[1] - text_width) // 2
    text_y = (frame.shape[0] + text_height) // 2
    cv2.rectangle(frame, (text_x, text_y - text_height - 10), (text_x + text_width, text_y + 10), (0, 0, 0), -1)
    cv2.putText(frame, text, (text_x, text_y), font, font_scale, color, thickness, cv2.LINE_AA)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    
def process_video(clip_paths, output_path, caption=None, audio_path=None):
    print("Starting video processing...")
    clips = [VideoFileClip(path) for path in clip_paths]
    final_clip = concatenate_videoclips(clips, method="compose")

    if caption:
        print("Adding caption...")
        final_clip = final_clip.fl_image(lambda frame: add_text_with_opencv(frame, caption))

    if audio_path:
        print("Adding audio overlay...")
        audio_clip = AudioFileClip(audio_path).set_duration(final_clip.duration)
        final_clip = final_clip.set_audio(audio_clip)

    print(f"Writing final video to {output_path}...")
    final_clip.write_videofile(output_path, codec='libx264', audio_codec='aac', verbose=False)
    final_clip.close()
    print("Video processing complete. Output saved to:", output_path)
    return {"path": output_path}

def save_clip(video_path, scene_info, output_directory, scene_id):
    # Ensure the output directory exists
    os.makedirs(output_directory, exist_ok=True)
    
    output_filename = f"scene_{scene_id+1}_{scene_info['category'].replace(' ', '_')}.mp4"
    output_filepath = os.path.join(output_directory, output_filename)
    
    try:
        start_seconds = convert_timestamp_to_seconds(scene_info['start_time'])
        end_seconds = convert_timestamp_to_seconds(scene_info['end_time'])
    
        video_clip = VideoFileClip(video_path).subclip(start_seconds, end_seconds)
        video_clip.write_videofile(output_filepath, codec='libx264', audio_codec='aac', verbose=False, logger=None)
        video_clip.close()

        # Check if the file was actually created
        if not os.path.exists(output_filepath):
            logging.error(f"Failed to save the clip at {output_filepath}")
            return None

        return {"path": output_filepath}
    
    except Exception as e:
        logging.error(f"An error occurred while saving the clip: {e}")
        return None


def convert_timestamp_to_seconds(timestamp):
    """Convert a timestamp in HH:MM:SS format to seconds."""
    h, m, s = map(float, timestamp.split(':'))
    return int(h) * 3600 + int(m) * 60 + s


