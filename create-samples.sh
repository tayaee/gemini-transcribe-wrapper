#!/bin/bash -x
uvx -q --with yt-dlp --with static-ffmpeg yt-dlp --remux-video mp4 --download-sections "*00:00-03:00" "https://www.youtube.com/watch?v=AqaOiXXaYaU" -o "samples/안될과학 개똥벌레.mp4"
uvx -q --with yt-dlp --with static-ffmpeg yt-dlp --remux-video mp4 --download-sections "*00:00-03:00" "https://www.youtube.com/watch?v=sJ1q9daiOpI" -o "samples/Jackson Hall Speech 20260828.mp4"
