# iranpost-mcp — رهگیری مرسولات شرکت ملی پست ایران

MCP server برای رهگیری مرسولات از `tracking.post.ir`.

## ابزارها
- `track(code)` — رهگیری یک کد 13/14/24 رقمی
- `track_many(codes)` — چند کد باهم (جدا شده با ویرگول/فاصله)
- `status()` — وضعیت پروکسی و زیرساخت

## نصب

```bash
# 1) وابستگی‌ها (فقط برای خواندن کپچا)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# دقت: server.py دایرکتوری site-packages را کنار خودش لود می‌کند
# (site.addsitedir) — پس site-packages باید کنار server.py باشد:
mv .venv/lib/python3.11/site-packages ./site-packages

# 2) ثبت در Hermes
hermes mcp add iranpost --command python3 --args $PWD/server.py
hermes mcp list        # باید iranpost با ✓ enabled ببینی
# برای فعال‌شدن ابزارها: سشن جدید یا ریستارت gateway
```

## نکته‌های مهم

1. **محدودیت جغرافیایی:** `tracking.post.ir` فقط با IP ایران جواب می‌ده
   (تأیید با check-host: گره‌های ایران 200 در 0.13s، همه گره‌های خارجی timeout).
   - داخل ایران: مستقیم وصل می‌شود (سرور اول direct را امتحان می‌کند).
   - خارج ایران: خودش از لیست‌های عمومی (ProxyScrape / GeoNode / proxifly)
     پروکسی ایرانی پیدا، موازی probe و بلافاصله استفاده می‌کند.
     پروکسی‌های رایگان چند دقیقه‌ای می‌میرند → نتیجه گاهی قطعی است.
   - راه‌حل قطعی: پروکسی ثابت ایرانی بدهید:
     `IRAN_PROXY=http://IP:PORT` (چندتا با ویرگول) به env سرور اضافه کنید.

2. **کپچای ۴ رقمی ASP.NET** روی فرم جستجو الزامی است. با
   `ddddocr.DdddOcr(show_ad=False, beta=True)` محلی حل می‌شود.
   مدل پیش‌فرض جواب نمی‌ده (`7101` را `T1ot` می‌خواند) و tesseract هم بی‌فایده است.

3. جریان سایت (ASP.NET WebForms):
   - `GET /search.aspx?id=CODE` → برداشت `__VIEWSTATE` / `__EVENTVALIDATION`
   - `GET /search.aspx?captcha=1&t=<ms>` → PNG کپچا (با همان کوکی‌ها)
   - `POST /search.aspx?id=CODE` با `__EVENTTARGET=btnSearch` + `txtCaptcha` + فیلدهای viewstate
   - پارس `#pnlResult`: تاریخ در `.newtdheader col-lg-6`، ردیف در
     `.row.newrowdata` با ۴ سلول `.newtddata` = [شماره، شرح، موقعیت، ساعت]

4. سایت فقط ۶ ماه آخر وضعیت را نگه می‌دارد؛ «جزئیات بیشتر» شماره موبایل می‌خواهد.

## فایل‌ها
- `server.py` — سرور MCP (جز mcp و ddddocr وابستگی دیگری ندارد)
- `requirements.txt`
