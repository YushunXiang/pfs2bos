"""Required destination configuration and artifact selection rules."""
import os
from pathlib import Path

from dotenv import dotenv_values


def configure_scan(args):
    """Require a dotenv file and resolve CLI > environment > dotenv values."""
    env_path = Path(args.env_file) if args.env_file is not None else Path.cwd() / '.env'
    try:
        with env_path.open(encoding='utf-8') as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except FileNotFoundError:
        raise ValueError(
            f'Configuration file not found: {env_path}. '
            'Copy example.env to .env and fill in PFS2BOS_ROOT, '
            'PFS2BOS_REMOTE and PFS2BOS_MOUNT, or specify --env-file.'
        ) from None
    for name in ('root', 'remote', 'mount'):
        key = f'PFS2BOS_{name.upper()}'
        value = getattr(args, name)
        if value is None:
            value = os.environ.get(key, values.get(key))
        if value is None or not value.strip():
            raise ValueError(f'{key} must not be empty; fill in {key} in {env_path} (see example.env)')
        setattr(args, name, value)


EXCLUDED = {'.git', '.hg', '.svn', '.snapshot', '.snapshots', 'snapshots',
            '.venv', 'venv', 'env', 'envs', 'miniforge', 'miniforge3',
            'miniconda3', 'anaconda3', 'node_modules', '__pycache__',
            '.cache', 'cache', 'caches', '.conda', '.local', '.npm', '.cargo',
            '.rustup', '.vscode', '.idea', '.config', '.ssh', '.ipython',
            '.jupyter', '.ipynb_checkpoints', 'bos-cli', 'tools'}
EXTENSIONS = {
    'weights': {'.pt', '.pth', '.ckpt', '.safetensors', '.onnx', '.gguf', '.bin', '.h5', '.hdf5'},
    'dataset': {'.parquet', '.arrow', '.npy', '.npz', '.pkl', '.pickle', '.mcap', '.bag', '.tfrecord', '.tfrecords', '.rlds', '.hdf', '.feather', '.zarr', '.csv', '.tsv', '.jsonl'},
    'media': {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.gif', '.mp4', '.avi', '.mov', '.mkv', '.webm', '.wav', '.flac', '.mp3'},
    'archive': {'.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar', '.zst', '.lz4', '.tgz'},
}


def excluded(name):
    return name in EXCLUDED or name.startswith(('.codex', '.agents', '.pfs2bos', 'miniforge'))
