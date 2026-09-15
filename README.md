# راهنمای نسخه ویندوزی با رابط فارسی (NevisarMic.exe)

## اجرای بدون ساخت exe (ساده)
```powershell
python nevisar_mic_gui.py
```
فقط پایتون لازم است (بدون هیچ پکیج). `ffmpeg` را نصب کنید:
```powershell
winget install Gyan.FFmpeg
ffmpeg -version
```

## ساخت فایل exe
1. پوشه را به ویندوز منتقل کنید.
2. روی `build_windows.bat` دابل‌کلیک کنید.
3. خروجی: `dist\NevisarMic.exe`

دستی:
```powershell
pip install pyinstaller
pyinstaller --noconfirm NevisarMic.spec
```

> نکته: `ffmpeg.exe` داخل exe جاسازی نمی‌شود. یا روی سیستم نصب باشد، یا `ffmpeg.exe` را کنار `NevisarMic.exe` بگذارید — برنامه خودش پیدایش می‌کند.

## تنظیمات خودکار برنامه
- آدرس API پیش‌فرض: `http://192.168.19.54/api`، سورس پیش‌فرض: `Mic Test`
- با اولین اجرا: ffmpeg و میکروفون‌ها خودکار پیدا می‌شوند.
- یوزر/پسورد/سورس/میکروفون ذخیره می‌شود: `%APPDATA%\NevisarMic\config.json`
- تیک «ذخیره رمز» = اتصال کاملاً خودکار دفعه بعد (بدون سوال).
- تیک «شروع خودکار» = با باز شدن برنامه استریم شروع می‌شود.
- هر بار دکمه شروع، کلید تازه از سرور گرفته می‌شود (چرخش کلید خودکار پوشش داده می‌شود) و اگر سرور در دسترس نباشد از آدرس کش‌شده استفاده می‌کند.
- فقط یک نفر همزمان استریم کند (تک‌پابلیشر).

---

# راهنمای کوتاه استریم میکروفون با Nevisar (نسخه متنی)

## نصب
1. `ffmpeg` را نصب کنید و مطمئن شوید در `PATH` است:
   ```powershell
   ffmpeg -version
   ```
2. فایل `stream_mic.py` را داشته باشید:
   ```powershell
   python stream_mic.py --help
   ```

## ترتیب استفاده
1. اول تنظیم اولیه:
   ```powershell
   python stream_mic.py --setup
   ```
2. بعد در پنل Nevisar سورس `Mic Test` را فعال کنید (گیرنده/استریم رسیور را روشن کنید).
3. بعد اسکریپت را اجرا کنید:
   ```powershell
   python stream_mic.py
   ```

## تنظیم اول (`--setup`)
فقط بار اول (یا وقتی پسورد/آدرس عوض شد):

```powershell
python stream_mic.py --setup
```

از شما می‌پرسد:
- آدرس API (پیش‌فرض: `http://192.168.19.54/api`)
- یوزرنیم ادمین (مدیریت live source)
- پسورد
- نام سورس (پیش‌فرض: `Mic Test`)

اطلاعات در فایل زیر ذخیره می‌شود (بدون پسورد):
- ویندوز: `C:\Users\ADMIN\.nevisar_mic_stream.json`
- لینوکس/مک: `~/.nevisar_mic_stream.json`

> پسورد ذخیره نمی‌شود. برای اتصال خودکار بدون سوال، قبل از اجرا ست کنید:
> ```powershell
> $env:NEVISAR_PASSWORD="پسورد"
> ```

## استفاده روزمره
لیست میکروفون‌ها:
```powershell
python stream_mic.py --list
```

شروع استریم (میکروفون را انتخاب کنید):
```powershell
python stream_mic.py
python stream_mic.py --mic 2
```

## حالت دستی (بدون لاگین)
```powershell
python stream_mic.py --url rtmp://192.168.19.54:1935/live/KEY
python stream_mic.py --host 192.168.19.54 --port 1935 --app live --key KEY
```

## مشکل وصل نشدن به `1935`
اگر خطای `Cannot open connection tcp://...:1935` گرفتید:
1. به همان شبکه/VPN وصل باشید (`ping 192.168.19.54`)
2. پورت `1935` روی سرور باز باشد
3. فقط یک نفر همزمان استریم کند (تک‌پابلیشر)
4. اگر کی عوض شده: دوباره `python stream_mic.py --setup` بزنید
