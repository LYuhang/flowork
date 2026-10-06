# -*- coding: utf-8 -*-

import ast
import re
import io
import os
import base64

from PIL import Image

from .public_http import download_public_bytes

IMAGE_PLACEHOLDER = os.environ.get("IMAGE_PLACEHOLDER", "<<image>>")
VIDEO_PLACEHOLDER = os.environ.get("VIDEO_PLACEHOLDER", "<<video>>")
AUDIO_PLACEHOLDER = os.environ.get("AUDIO_PLACEHOLDER", "<<audio>>")

_REMOTE_IMAGE_MAX_BYTES = 20 * 1024 * 1024


def _download_public_image(url: str) -> bytes:
    return download_public_bytes(
        url,
        max_bytes=_REMOTE_IMAGE_MAX_BYTES,
        label="remote image",
    )


def encode_image(image_path_or_bytes, force_jpeg=False, return_base64=True):
    if isinstance(image_path_or_bytes, str):
        if image_path_or_bytes.startswith("http"):
            image_bytes = _download_public_image(image_path_or_bytes)
        else:
            if image_path_or_bytes.startswith("b'\\x"):
                # String representation of bytes.
                image_bytes = ast.literal_eval(image_path_or_bytes)
                if not isinstance(image_bytes, bytes):
                    raise ValueError("serialized image value must decode to bytes")
            else:
                assert os.path.exists(image_path_or_bytes), f"Image path: {image_path_or_bytes} does not exist!"
                with open(image_path_or_bytes, "rb") as fp:
                    image_bytes = fp.read()
    else:
        assert isinstance(image_path_or_bytes, bytes), "Only support image type `string` and `bytes`, but get `{}`".format(
            type(image_path_or_bytes)
        )
        image_bytes = image_path_or_bytes

    if force_jpeg:
        image = Image.open(io.BytesIO(image_bytes))
        jpeg_bytes_io = io.BytesIO()
        image.save(jpeg_bytes_io, format='JPEG')
        image_bytes = jpeg_bytes_io.getvalue()

    if return_base64:
        return base64.b64encode(image_bytes).decode('utf-8')
    else:
        return image_bytes


def convert_input(data_dict, min_pixels, max_pixels):
    messages = []

    if "system" in data_dict and data_dict["system"]:
        messages.append({
            "role": "system",
            "content": data_dict["system"],
        })

    image = list(data_dict.get("image", []))
    audio = list(data_dict.get("audio", []))
    video = list(data_dict.get("video", []))
    for conv in data_dict["conversations"]:
        if conv["from"] == "human":
            if IMAGE_PLACEHOLDER in conv["value"] or \
                AUDIO_PLACEHOLDER in conv["value"] or \
                    VIDEO_PLACEHOLDER in conv["value"]:
                assert len(image) == conv["value"].count(IMAGE_PLACEHOLDER), (
                    "The number of images "
                    f"{len(data_dict['image'])} is not equal to the number of "
                    f"{conv['value'].count(IMAGE_PLACEHOLDER)} placeholders in the text."
                )
                assert len(audio) == conv["value"].count(AUDIO_PLACEHOLDER), (
                    "The number of audios "
                    f"{len(data_dict['audio'])} is not equal to the number of "
                    f"{conv['value'].count(AUDIO_PLACEHOLDER)} placeholders in the text."
                )
                assert len(video) == conv["value"].count(VIDEO_PLACEHOLDER), (
                    "The number of videos "
                    f"{len(data_dict['video'])} is not equal to the number of "
                    f"{conv['value'].count(VIDEO_PLACEHOLDER)} placeholders in the text."
                )

                split_list = re.split(r'({}|{}|{})'.format(IMAGE_PLACEHOLDER, AUDIO_PLACEHOLDER, VIDEO_PLACEHOLDER), conv["value"])
                split_list = [s for s in split_list if s]

                content_list = []
                for split_str in split_list:
                    if split_str == IMAGE_PLACEHOLDER:
                        image_bytes = encode_image(image.pop(0), return_base64=False)
                        with Image.open(io.BytesIO(image_bytes)) as decoded:
                            mime_type = Image.MIME.get(decoded.format)
                        if not mime_type:
                            raise ValueError("Image format has no supported MIME type")
                        base64_image = base64.b64encode(image_bytes).decode("ascii")
                        content_list.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}",
                            },
                            "min_pixels": min_pixels,
                            "max_pixels": max_pixels,
                        })
                    elif split_str in [AUDIO_PLACEHOLDER, VIDEO_PLACEHOLDER]:
                        raise ValueError("Not supported!")
                    else:
                        content_list.append({
                            "type": "text",
                            "text": split_str
                        })
            else:
                content_list = conv["value"]

            messages.append({
                "role": "user",
                "content": content_list
            })
        elif conv["from"] == "gpt":
            messages.append({
                "role": "assistant",
                "content": conv["value"]
            })
        elif conv["from"] == "system":
            messages.append({
                "role": "system",
                "content": conv["value"].replace(
                    IMAGE_PLACEHOLDER, "IMAGE_PLACEHOLDER"
                ).replace(
                    AUDIO_PLACEHOLDER, "AUDIO_PLACEHOLDER"
                ).replace(
                    VIDEO_PLACEHOLDER, "VIDEO_PLACEHOLDER"
                ),
            })
        else:
            raise ValueError("No such role: {}".format(conv["from"]))

    return messages
