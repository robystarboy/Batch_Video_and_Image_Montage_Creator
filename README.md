<<<<<<< HEAD
# Batch_Video_and_Image_Montage_Creator
Creates video montages from folders of video clips and images. Appends a Polaroid-style image slideshow if images are present.
=======
# Batch Video and Image Montage Creator

This script batch-creates video montages from folders of video clips and images. It uses ffmpeg for video processing and Pillow for image handling. Only unique video clips and images are included in the final montage.

## Features
- Processes all subfolders in the script directory
- User can set montage length (default 60 seconds) at runtime
- Appends a Polaroid-style image slideshow if images are present
- Deduplicates similar videos and images (only unique clips/images are included in the final montage)
- Handles HEIC image conversion (macOS only)
- Progress bars and detailed status output

## Requirements
- Python 3.7+
- ffmpeg and ffprobe (must be installed and in your PATH)
- macOS: sips (for HEIC conversion, built-in)

### Python Dependencies
- Pillow
- imagehash
- certifi (recommended for SSL on macOS)

## Installation

1. **Install ffmpeg**
   - macOS: `brew install ffmpeg`
   - Windows: [Download from ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH
   - Linux: `sudo apt install ffmpeg`

2. **Install Python dependencies**

```sh
pip install -r requirements.txt
```

3. **(Optional) For best SSL compatibility on macOS:**

```sh
pip install certifi
```

## Usage

1. Place this script in a directory containing one or more subfolders. Each subfolder should contain video clips and/or images.
2. Run the script:

```sh
python make_all_montages.py
```

3. The script will process each subfolder, creating a `montage_<foldername>.mp4` in each.

## Supported File Types
- **Videos:** .mp4, .mov, .m4v, .avi, .mts, .mkv
- **Images:** .jpg, .jpeg, .png, .heic, .tiff, .tif, .bmp

## Example Directory Structure

```
Batch Video and Image Montage Creator/
├── make_all_montages.py
├── Trip1/
│   ├── clip1.mp4
│   ├── image1.jpg
│   └── ...
├── Trip2/
│   ├── ...
```

## License
MIT License
>>>>>>> 5d1296c (Initial commit)
