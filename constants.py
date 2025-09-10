# General limits
MAX_INPUT_LENGTH = 200
MAX_APPROX_TOKENS = 3500
MAX_OCR_TEXT_LENGTH = 1900
OCR_DAILY_LIMIT = 30

# Google Translate (global quota)
GOOGLE_TRANSLATE_DAILY_LIMIT = 15000

# Channels
AUTO_TTS_CHANNELS = {
    1405246031624667226,
    1396150984329396334,
}
TRANSLATION_CHANNELS = {
    1411181941792706610: "multi",
    1411203716408672377: "multi",
    1402856274206396566: ("en", "th"),
    1413467417912545280: ("th", "ja"),
    1400241948996141186: ("th", "en"),
    1396057794297204877: ("th", "en"),
    1396100631814471690: ("th", "zh"),
    1396100850966990938: ("th", "ko"),
    1396100885288976424: ("th", "ru"),
    1396410707175800944: ("th", "vi"),
    1398616266809282670: ("th", "ja"),
}
DETAILED_EN_CHANNELS = {1402856274206396566}
DETAILED_JA_CHANNELS = {1398616266809282670}

# File exts
AUDIO_EXTS = (
    ".wav", ".flac", ".mp3", ".m4a", ".aac",
    ".ogg", ".opus", ".webm"
)

# Lang normalization for Google Translate
GOOGLE_LANG_MAP = {
    "zh": "zh-CN",
    "zh-CN": "zh-CN",
    "jp": "ja",
}
