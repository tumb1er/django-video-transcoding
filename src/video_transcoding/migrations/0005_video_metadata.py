# Generated by Django 5.0.6 on 2024-06-26 07:24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('video_transcoding', '0004_audioprofile_audiotrack_preset_audioprofiletracks_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='video',
            name='metadata',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
