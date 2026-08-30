echo ---- prev ----
DIR=/rosenas/data/video/KTV/Archive/AIML/FastCampus/LLM모델파인튜닝을위한양자화-이승유
MP4="1.1.1 LLM과 Quantization의 기초, 강의 소개, 강의 흐름 및 강의 소개.mp4"
ls -l $DIR/1.1.1*
/bin/rm -f $DIR/$MP4
uv -q tool install -e . --force
gtw --version
gtw "$DIR/$MP4"
ls -l $DIR/1.1.1*
