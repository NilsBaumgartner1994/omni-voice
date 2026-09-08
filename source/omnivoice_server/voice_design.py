"""Voice-design attribute catalogue exposed to the web UI.

The wording mirrors what the upstream model was trained on: accents are
given in English, Chinese dialects in Chinese.
"""

from __future__ import annotations

VOICE_DESIGN_CATEGORIES: list[dict[str, object]] = [
    {
        "key": "gender",
        "label": "Geschlecht",
        "options": [
            {"value": "male", "label": "männlich"},
            {"value": "female", "label": "weiblich"},
        ],
    },
    {
        "key": "age",
        "label": "Alter",
        "options": [
            {"value": "child", "label": "Kind"},
            {"value": "teenager", "label": "Jugendlich"},
            {"value": "young adult", "label": "Junger Erwachsener"},
            {"value": "middle-aged", "label": "Mittleres Alter"},
            {"value": "elderly", "label": "Älter"},
        ],
    },
    {
        "key": "pitch",
        "label": "Tonhöhe",
        "options": [
            {"value": "very low pitch", "label": "sehr tief"},
            {"value": "low pitch", "label": "tief"},
            {"value": "moderate pitch", "label": "mittel"},
            {"value": "high pitch", "label": "hoch"},
            {"value": "very high pitch", "label": "sehr hoch"},
        ],
    },
    {
        "key": "style",
        "label": "Stil",
        "options": [{"value": "whisper", "label": "Flüstern"}],
    },
    {
        "key": "accent",
        "label": "Englischer Akzent",
        "hint": "Wirkt nur bei englischem Text.",
        "options": [
            {"value": "american accent", "label": "amerikanisch"},
            {"value": "australian accent", "label": "australisch"},
            {"value": "british accent", "label": "britisch"},
            {"value": "canadian accent", "label": "kanadisch"},
            {"value": "chinese accent", "label": "chinesisch"},
            {"value": "indian accent", "label": "indisch"},
            {"value": "japanese accent", "label": "japanisch"},
            {"value": "korean accent", "label": "koreanisch"},
            {"value": "portuguese accent", "label": "portugiesisch"},
            {"value": "russian accent", "label": "russisch"},
        ],
    },
    {
        "key": "dialect",
        "label": "Chinesischer Dialekt",
        "hint": "Wirkt nur bei chinesischem Text.",
        "options": [
            {"value": "河南话", "label": "Henan"},
            {"value": "陕西话", "label": "Shaanxi"},
            {"value": "四川话", "label": "Sichuan"},
            {"value": "贵州话", "label": "Guizhou"},
            {"value": "云南话", "label": "Yunnan"},
            {"value": "桂林话", "label": "Guilin"},
            {"value": "济南话", "label": "Jinan"},
            {"value": "石家庄话", "label": "Shijiazhuang"},
            {"value": "甘肃话", "label": "Gansu"},
            {"value": "宁夏话", "label": "Ningxia"},
            {"value": "青岛话", "label": "Qingdao"},
            {"value": "东北话", "label": "Nordost"},
        ],
    },
]
