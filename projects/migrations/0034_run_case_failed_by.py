from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0033_screenshot_attempt_evidence"),
    ]

    operations = [
        migrations.AddField(
            model_name="testruntestcase",
            name="failed_by",
            field=models.CharField(
                blank=True,
                choices=[
                    ("agent", "AI verdict"),
                    ("help", "Marked failed (help)"),
                    ("review", "Changed after review"),
                ],
                default="",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="testrun",
            name="night_digest_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
