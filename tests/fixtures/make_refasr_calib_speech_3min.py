"""Synthesize a 195 s read-back style speech clip at 8 kHz with Fish s2.1-pro-free (0 USD) for the
reference-ASR calibration stage: one Fish call per sentence, 800 ms of silence between sentences, so
every sentence is one utterance for the playout segmenter (500 ms hold). Writes wav, txt, json
(word times and per-sentence onset/end)."""
import json
from pathlib import Path
import numpy as np, soundfile as sf
from dotenv import load_dotenv
from duplexbench.stimuli.fish_tts import FishTTS, load_voices

ROOT = Path(r"C:\Users\Lenovo\research\duplex-bench")
load_dotenv(ROOT / ".env")
OUT = ROOT / "tests" / "fixtures" / "refasr_calib_speech_3min"
SR, PAUSE_MS, LEAD_MS, TOTAL_S = 8000, 800, 500, 195
SENTENCES = [
 "Thank you for calling. Before I make any change, let me read back what I have.",
 "The phone number on file is four one five, five five five, zero one four two.",
 "The appointment is on Tuesday, November eleventh, at ten thirty in the morning, with Doctor Patel.",
 "Your account balance is one hundred and twenty dollars, and the refund cap is fifty dollars.",
 "The zip code is nine four one zero seven, dash two two one eight.",
 "The confirmation code is R T, four four seven one.",
 "If anything in that is wrong, please stop me and I will correct it.",
 "One moment while I check the system.",
 "The payment did not go through, so nothing was charged to your card.",
 "I can refund up to the cap today, and a supervisor can approve more.",
 "Could you spell your email address for me, one letter at a time?",
 "I have updated the address to twelve Market Street, apartment four.",
 "Is there anything else I can help you with before we end the call?",
 "Thank you, and have a good day.",
 "Let me pull up the account ending in seven seven, one moment please.",
 "I see two appointments: one with Doctor Lee on Wednesday at eleven, and one with Doctor Patel on Thursday at two.",
 "To verify your identity, I need your date of birth and the zip code on the account.",
 "I have July fourteenth, nineteen fifty eight, and six zero six one four. Is that right?",
 "The last payment of forty two dollars and ten cents posted on November third.",
 "I can move the Wednesday visit to Friday at nine fifteen, if that works for you.",
 "Your new confirmation number is A P, one zero three.",
 "The reference for this call is nine nine two, dash four four one.",
 "Please hold for a moment while the system processes the refund.",
 "The refund of fifty dollars will reach your card in three to five business days.",
 "I am sorry, I did not catch that. Could you repeat the last four digits?",
 "I have three one two, five five five, zero one one one as the callback number.",
 "The office is at two hundred Main Street, suite three hundred, and parking is behind the building.",
 "You can bring a photo ID and your insurance card to the first visit.",
 "The co-pay is twenty five dollars, due at check in.",
 "I have cancelled the Thursday appointment and kept the Friday one.",
 "Would you like a text reminder the day before, or an email?",
 "The email on file is leo dot park at example dot com.",
 "I will send the summary to that address in the next few minutes.",
 "There is a late fee of fifteen dollars on the account, and I can waive it once.",
 "The balance after the waiver is one hundred and five dollars.",
 "Your account number is A C, dash seven seven.",
 "The next statement closes on the twentieth, and payment is due on the fifth.",
 "I have noted that you prefer morning appointments.",
 "If you need to reach us after hours, the line is open until eight in the evening.",
 "Let me repeat the time once more: Friday, November fourteenth, at nine fifteen in the morning.",
 "Is there anything in that summary I should change?",
 "Thank you for your patience today, and take care.",
]
tts = FishTTS(model="s2.1-pro-free", sample_rate=SR, latency="normal",
              voices=load_voices(ROOT / "configs" / "voices" / "fish.yaml"), default_voice="delia")
parts, words_all, utts = [np.zeros(LEAD_MS * SR // 1000, dtype=np.int16)], [], []
cursor_ms = LEAD_MS
for i, s in enumerate(SENTENCES):
    pcm, sr, words = tts.synthesize(s, "delia", 1.0)
    assert sr == SR, sr
    # trim leading/trailing silence below -45 dBFS in 20 ms frames so the pause is exactly PAUSE_MS
    x = np.asarray(pcm, dtype=np.int16); f = SR * 20 // 1000
    rms = np.array([np.sqrt(np.mean(x[k:k + f].astype(np.float64) ** 2)) for k in range(0, len(x) - f + 1, f)])
    act = np.where(20 * np.log10(np.maximum(rms, 1) / 32768) > -45)[0]
    a, b = (act[0] * f, min(len(x), (act[-1] + 1) * f)) if len(act) else (0, len(x))
    off_ms = a * 1000 / SR
    seg = x[a:b]
    parts.append(seg)
    words_all += [{"text": w.text, "start_ms": int(round(w.start_ms - off_ms + cursor_ms)),
                   "end_ms": int(round(w.end_ms - off_ms + cursor_ms))} for w in words]
    utts.append({"i": i, "text": s, "onset_ms": cursor_ms, "end_ms": cursor_ms + int(len(seg) * 1000 / SR)})
    cursor_ms += int(len(seg) * 1000 / SR)
    parts.append(np.zeros(PAUSE_MS * SR // 1000, dtype=np.int16)); cursor_ms += PAUSE_MS
    print(f"{i:2d} {len(seg)/SR:5.2f}s {len(words):3d}w  {s[:50]}")
y = np.concatenate(parts)
print(f"speech+pauses {len(y)/SR:.1f} s")
target = TOTAL_S * SR
y = np.concatenate([y, np.zeros(target - len(y), dtype=np.int16)]) if len(y) < target else y[:target]
sf.write(str(OUT.with_suffix(".wav")), y, SR, subtype="PCM_16")
OUT.with_suffix(".txt").write_text(" ".join(SENTENCES) + "\n", encoding="utf-8")
OUT.with_suffix(".json").write_text(json.dumps({"vendor": tts.version, "voice": "delia", "sample_rate": SR,
    "pause_ms": PAUSE_MS, "lead_ms": LEAD_MS, "duration_s": TOTAL_S, "n_sentences": len(SENTENCES),
    "utterances": utts, "words": words_all}, indent=1), encoding="utf-8")
print("written", OUT.with_suffix(".wav"), "utterances", len(utts), "words", len(words_all))
