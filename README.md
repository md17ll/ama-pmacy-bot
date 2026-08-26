# بوت صيدليات عامودا المناوبة

نسخة كاملة قابلة للنشر مباشرة على Railway.

## ما الموجود؟

- واجهة المستخدم: المناوبة الآن، اليوم، غداً، البحث عن صيدلية، تحديث الوقت.
- عرض الاسم والعنوان ووقت المناوبة بنظام 12 ساعة.
- لوحة إدارة محمية حسب Telegram User ID.
- إدارة الصيدليات والمناوبات والمسودات والأدمن.
- قراءة صور الجداول بواسطة Gemini.
- استيراد وتصدير Excel.
- فحص التواريخ والأوقات والتكرار والتداخل.
- سجل عمليات، تراجع، نسخ احتياطية وإحصائيات.
- إشعار أول دخول لمستخدم جديد.
- PostgreSQL ودعم Railway.

## طريقة البناء

المشروع الكامل محفوظ داخل:

```text
.bootstrap/project.zip
```

يقوم `Dockerfile` بفك النسخة الكاملة أثناء بناء Railway ثم يثبت المكتبات ويشغّل `main.py`.

## متغيرات Railway

```env
BOT_TOKEN=
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openai/gpt-5.4-mini
DATABASE_URL=
OWNER_IDS=
TIMEZONE=Asia/Damascus
RUN_MODE=polling
WEBHOOK_BASE_URL=
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_SECRET=
LOG_LEVEL=INFO
```

لا تضع التوكنات داخل GitHub. أضفها فقط في Railway Variables.

## تشغيل موفر عبر Webhook وServerless

التطبيق يدعم `polling` للتشغيل الدائم و`webhook` للتشغيل القابل للنوم. لاستخدام
Railway Serverless مع الحفاظ على حالات المحادثات بعد إعادة التشغيل:

1. أنشئ Public Domain لخدمة البوت في Railway.
2. اضبط المتغيرات التالية في خدمة البوت:

   ```env
   RUN_MODE=webhook
   WEBHOOK_BASE_URL=https://YOUR-SERVICE.up.railway.app
   WEBHOOK_PATH=/telegram/webhook
   WEBHOOK_SECRET=CHANGE_TO_A_RANDOM_SECRET
   ```

3. فعّل `Settings > Deploy > Serverless` لخدمة البوت.
4. استخدم PostgreSQL في `DATABASE_URL`. حالات محادثات aiogram محفوظة في نفس
   القاعدة، واتصالات وضع webhook تُغلق بعد كل عملية حتى تستطيع الخدمة النوم.
5. أنشئ Railway Cron service من المستودع نفسه بالأمر:

   ```text
   python -m app.expiry_job
   ```

   واضبط الجدول `0 * * * *` وشارك معه `BOT_TOKEN` و`DATABASE_URL` و`OWNER_IDS`
   و`TIMEZONE` ومتغيرات webhook. المهمة تفحص تنبيه انتهاء الجدول مرة واحدة ثم
   تنتهي، بدلاً من إبقاء خدمة البوت مستيقظة طوال الساعة.

للرجوع السريع، عطّل Serverless وأعد `RUN_MODE=polling`؛ لا حاجة لتغيير الكود أو
قاعدة البيانات.

## الفحص

تم فحص النسخة محلياً:

```text
19 passed, 2 skipped
```

كما نجح فحص تركيب ملفات Python بالكامل.
