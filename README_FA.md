# AirSat Public Runner → private airsat-auto

این مخزن فقط موتور اجرای عمومی است. داده‌های تولیدشده مستقیماً در مخزن خصوصی زیر نوشته و Commit می‌شوند:

`Attarbashian/airsat-auto`، شاخه `main`

مسیرهای خروجی بدون تغییر:

- `public/data/`
- `public/visual_real/`

## Secrets لازم در مخزن عمومی

- `AIRSAT_AUTO_PAT`
- `EE_SERVICE_ACCOUNT_JSON`
- `EE_PROJECT`
- `EE_PROVINCES_ASSET`
- `EE_PROVINCE_NAME_FIELD`

## ترتیب اولین آزمایش

1. `00 - Test AirSat Connections`
2. `01 - Update Dynamic Layers to airsat-auto` با `NO2`
3. پس از تأیید خروجی، `03 - Rebuild Time Series to airsat-auto` با `NO2`
4. سپس `02 - Bootstrap Archive to airsat-auto` و `04 - Build Multi-Year Ranges to airsat-auto`

اسکریپت‌ها با متغیر `AIRSAT_REPOSITORY_ROOT` مستقیماً داخل checkout مخزن مقصد می‌نویسند. فایل‌های رابط پنل در مخزن مقصد تغییر نمی‌کنند؛ Commit فقط مسیرهای داده و تصاویر تولیدی را stage می‌کند.
