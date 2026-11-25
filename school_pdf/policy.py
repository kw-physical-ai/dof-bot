"""Small task-conditioned categorical BC baseline. No robot dependencies."""
from __future__ import annotations

import cv2
import numpy as np
import torch
from torch import nn

CLASSES = ('paper', 'plastic', 'vinyl')
LIMITS = ((0, 180), (0, 180), (0, 180), (0, 180), (0, 270), (0, 180))


def preprocess_image(path, image_size=96):
    """Decode BGR, letterbox in black, and return RGB CHW float in [0,1]."""
    frame = cv2.imread(str(path))
    if frame is None:
        raise ValueError(f'Cannot decode image: {path}')
    height, width = frame.shape[:2]
    scale = image_size / max(height, width)
    resized = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))
    canvas = np.zeros((image_size, image_size, 3), dtype=np.uint8)
    y, x = (image_size-resized.shape[0])//2, (image_size-resized.shape[1])//2
    canvas[y:y+resized.shape[0], x:x+resized.shape[1]] = resized[:, :, ::-1]
    return torch.from_numpy(canvas.transpose(2, 0, 1).copy()).float().div_(255)


def resolve_device(name='auto'):
    available = {'cpu': True, 'cuda': torch.cuda.is_available(),
                 'mps': bool(hasattr(torch.backends, 'mps') and torch.backends.mps.is_available())}
    if name == 'auto':
        name = next(key for key in ('cuda', 'mps', 'cpu') if available[key])
    if name not in available or not available[name]:
        raise ValueError(f'Device unavailable: {name}; availability={available}')
    return torch.device(name)


class Policy(nn.Module):
    def __init__(self, camera_mode='csi', image_size=96):
        super().__init__()
        if camera_mode not in ('csi', 'dual'):
            raise ValueError('camera_mode must be csi or dual')
        if image_size < 16:
            raise ValueError('image_size must be at least 16')
        self.camera_mode, self.image_size = camera_mode, image_size
        self.encoder = nn.Sequential(
            nn.Conv2d(3, 16, 5, stride=2, padding=2), nn.ReLU(),
            nn.Conv2d(16, 32, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(32, 48, 3, stride=2, padding=1), nn.ReLU(),
            nn.AdaptiveAvgPool2d((2, 2)), nn.Flatten())
        self.classifier = nn.Linear(192, 3)
        self.task_embedding = nn.Embedding(3, 12)
        self.actions = nn.Sequential(nn.Linear(192 * (2 if camera_mode == 'dual' else 1) + 18, 128),
                                     nn.ReLU(), nn.Linear(128, 18))
        self.register_buffer('joint_max', torch.tensor([hi for _, hi in LIMITS], dtype=torch.float32))

    def forward(self, images, measured, task_ids):
        cameras = 2 if self.camera_mode == 'dual' else 1
        if images.ndim != 5 or images.shape[1:3] != (cameras, 3):
            raise ValueError(f'Expected [B,{cameras},3,H,W] images')
        features = self.encoder(images.flatten(0, 1)).reshape(images.shape[0], cameras, 192)
        # CSI is always first. Wrist features never reach the material classifier.
        classes = self.classifier(features[:, 0])
        state = measured / self.joint_max * 2 - 1
        joint_input = torch.cat((features.flatten(1), state, self.task_embedding(task_ids)), dim=1)
        return self.actions(joint_input).reshape(-1, 6, 3), classes
