import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pyarrow as pa
import torch
import torchvision
from datasets.features.features import register_feature


def load_from_videos(
    item: dict[str, torch.Tensor], video_frame_keys: list[str], videos_dir: Path, tolerance_s: float
):
    # since video path already contains "videos" (e.g. videos_dir="data/videos", path="videos/episode_0.mp4")
    data_dir = videos_dir.parent

    for key in video_frame_keys:
        if isinstance(item[key], list):
            # load multiple frames at once (expected when delta_timestamps is not None)
            timestamps = [frame["timestamp"] for frame in item[key]]
            paths = [frame["path"] for frame in item[key]]
            if len(set(paths)) == 1:
                raise NotImplementedError("All video paths are expected to be the same for now.")
            video_path = data_dir / paths[0]

            frames = decode_video_frames_torchvision(video_path, timestamps, tolerance_s)
            item[key] = frames
        else:
            # load one frame
            timestamps = [item[key]["timestamp"]]
            video_path = data_dir / item[key]["path"]

            frames = decode_video_frames_torchvision(video_path, timestamps, tolerance_s)
            item[key] = frames[0]

    return item


def decode_video_frames_torchvision(
    video_path: str,
    timestamps: list[float],
    tolerance_s: float,
    device: str = "cpu",
    log_loaded_timestamps: bool = False,
):
    """Loads frames associated to the requested timestamps of a video

    Note: Video benefits from inter-frame compression. Instead of storing every frame individually,
    the encoder stores a reference frame (or a key frame) and subsequent frames as differences relative to
    that key frame. As a consequence, to access a requested frame, we need to load the preceeding key frame,
    and all subsequent frames until reaching the requested frame. The number of key frames in a video
    can be adjusted during encoding to take into account decoding time and video size in bytes.
    """
    video_path = str(video_path)

    # set backend
    if device == "cpu":
        # explicitely use pyav
        torchvision.set_video_backend("pyav")
    elif device == "cuda":
        # TODO(rcadene, aliberts): implement video decoding with GPU
        # torchvision.set_video_backend("cuda")
        # torchvision.set_video_backend("video_reader")
        # requires installing torchvision from source, see: https://github.com/pytorch/vision/blob/main/torchvision/csrc/io/decoder/gpu/README.rst
        # check possible bug: https://github.com/pytorch/vision/issues/7745
        raise NotImplementedError()
    else:
        raise ValueError(device)

    # set a video stream reader
    # TODO(rcadene): also load audio stream at the same time
    reader = torchvision.io.VideoReader(video_path, "video")

    # set the first and last requested timestamps
    # Note: previous timestamps are usually loaded, since we need to access the previous key frame
    first_ts = timestamps[0]
    last_ts = timestamps[-1]

    # access closest key frame of the first requested frame
    # Note: closest key frame timestamp is usally smaller than `first_ts` (e.g. key frame can be the first frame of the video)
    # for details on what `seek` is doing see: https://pyav.basswood-io.com/docs/stable/api/container.html?highlight=inputcontainer#av.container.InputContainer.seek
    reader.seek(first_ts)

    # load all frames until last requested frame
    loaded_frames = []
    loaded_ts = []
    for frame in reader:
        current_ts = frame["pts"]
        if log_loaded_timestamps:
            logging.info(f"frame loaded at timestamp={current_ts:.4f}")
        loaded_frames.append(frame["data"])
        loaded_ts.append(current_ts)
        if current_ts >= last_ts:
            break

    query_ts = torch.tensor(timestamps)
    loaded_ts = torch.tensor(loaded_ts)

    # compute distances between each query timestamp and timestamps of all loaded frames
    dist = torch.cdist(query_ts[:, None], loaded_ts[:, None], p=1)
    min_, argmin_ = dist.min(1)

    is_within_tol = min_ < tolerance_s
    assert is_within_tol.all(), (
        f"One or several query timestamps unexpectedly violate the tolerance ({min_[~is_within_tol]} > {tolerance_s=})."
        "It means that the closest frame that can be loaded from the video is too far away in time."
        "This might be due to synchronization issues with timestamps during data collection."
        "To be safe, we advise to ignore this item during training."
    )

    # get closest frames to the query timestamps
    closest_frames = torch.stack([loaded_frames[idx] for idx in argmin_])
    closest_ts = loaded_ts[argmin_]

    if log_loaded_timestamps:
        logging.info(f"{closest_ts=}")

    # convert to the pytorch format which is float32 in [0,1] range (and channel first)
    closest_frames = closest_frames.type(torch.float32) / 255

    assert len(timestamps) == len(closest_frames)
    return closest_frames


def encode_video_frames(imgs_dir: Path, video_path: Path, fps: int):
    # For more info this setting, see: `lerobot/common/datasets/_video_benchmark/README.md`
    video_path = Path(video_path)
    video_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_cmd = (
        f"ffmpeg -r {fps} "
        "-f image2 "
        "-loglevel error "
        f"-i {str(imgs_dir / 'frame_%06d.png')} "
        "-vcodec libx264 "
        "-pix_fmt yuv444p "
        f"{str(video_path)}"
    )
    subprocess.run(ffmpeg_cmd.split(" "), check=True)


@dataclass
class VideoFrame:
    # TODO(rcadene, lhoestq): move to Hugging Face `datasets` repo
    """
    Provides a type for a dataset containing video frames.

    Example:

    ```python
    data_dict = [{"image": {"path": "videos/episode_0.mp4", "timestamp": 0.3}}]
    features = {"image": VideoFrame()}
    Dataset.from_dict(data_dict, features=Features(features))
    ```
    """

    pa_type: ClassVar[Any] = pa.struct({"path": pa.string(), "timestamp": pa.float32()})
    _type: str = field(default="VideoFrame", init=False, repr=False)

    def __call__(self):
        return self.pa_type


# to make it available in HuggingFace `datasets`
register_feature(VideoFrame, "VideoFrame")
