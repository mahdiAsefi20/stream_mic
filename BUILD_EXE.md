# ساخت فایل اجرایی ویندوز (.exe)

## پیش‌نیازها

1. **Python 3.10+** — از [python.org](https://www.python.org/downloads/) دانلود کنید. موقع نصب تیک **"Add Python to PATH"** را بزنید.
2. **ffmpeg** — لازم است روی سیستم نصب باشد یا کنار exe قرار بگیرد:
   ```powershell
   winget install Gyan.FFmpeg
   ```
   یا ffmpeg.exe را از [ffmpeg.org/download](https://ffmpeg.org/download.html) دانلود کرده و کنار فایل خروجی بگذارید.

## روش ساده (پیشنهادی)

فایل `build_windows.bat` را در پوشه پروژه پیدا کنید و روی آن **دابل‌کلیک** کنید. همه چیز خودکار انجام می‌شود:

```
stream_mic/
├── build_windows.bat    ← این را اجرا کنید
├── nevisar_mic_gui.py
├── NevisarMic.spec
└── dist/
    └── NevisarMic.exe   ← خروجی اینجا ظاهر می‌شود
```

## روش دستی

اگر دوست دارید قدم به قدم اجرا کنید:

```powershell
# 1. وارد پوشه پروژه شوید
cd stream_mic

# 2. PyInstaller را نصب کنید
pip install pyinstaller

# 3. exe را بسازید
pyinstaller --noconfirm NevisarMic.spec

# 4. فایل خروجی
dir dist\NevisarMic.exe
```

## روش سریع (بدون .spec)

اگر می‌خواهید بدون فایل `.spec` بسازید:

```powershell
pyinstaller --noconfirm --onefile --windowed --name NevisarMic nevisar_mic_gui.py
```

## توضیح پارامترها

| پارامتر | معنی |
|----------|------|
| `--onefile` | همه چیز در یک فایل exe واحد |
| `--windowed` | بدون پنجره کنسول سیاه (فقط رابط گرافیکی) |
| `--noconfirm` | بدون سوال بازنویسی پوشه build/dist |
| `--name NevisarMic` | نام فایل خروجی |

## توزیع به کاربران نهایی

### فایل‌های لازم برای کپی روی کامپیوتر دیگر:

```
NevisarMic.exe          ← برنامه اصلی
ffmpeg.exe              ← (اختیاری اگر روی سیستم نصب نیست)
```

### نحوه استفاده:

1. فایل‌ها را در یک پوشه قرار دهید.
2. `NevisarMic.exe` را اجرا کنید.
3. یوزرنیم و رمز نویزار را وارد کنید (فقط یک بار).
4. دکمه **«شروع استریم»** را بزنید.

### نکات مهم:

- **ffmpeg لازم است** — یا با `winget install Gyan.FFmpeg` نصب کنید، یا `ffmpeg.exe` را کنار `NevisarMic.exe` بگذارید.
- **بدون پکیج نصبی** — نیازی به نصب پایتون یا pip روی کامپیوتر کاربر نیست.
- **تنظیمات خودکار** — با اولین اجرا ذخیره می‌شود و دفعه بعد فقط دکمه Start را بزنید.
- **تک‌پابلیشر** — فقط یک نفر همزمان می‌تواند استریم کند.

## عیب‌یابی

| مشکل | راه‌حل |
|------|--------|
| `ffmpeg not found` | ffmpeg را نصب کنید یا کنار exe بگذارید |
| `میکروفون پیدا نشد` | میکروفون USB را وصل کنید و دوباره اجرا کنید |
| `ورود ناموفق` | یوزرنیم/پسورد را چک کنید، حتماً با اکانت ادمین |
| `پورت 1935 در دسترس نیست` | به VPN/شبکه وصل باشید و پورت 1935 روی سرور باز باشد |
| آنتی‌ویروس فایل را حذف کرد | فایل را از قرنطینه خارج کنید (PyInstaller گاهی false positive دارد) |
