# Removes the "How to do it" guides and Ask AI (docs/plan/refactor-remove-howto-and-ask-ai.md).
# Data loss: every Ask AI question and answer, every manual step guide and the
# stored how-to list of every person step. Reversing recreates the empty tables
# and an empty ``how_to`` column.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0056_evidence_review"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="casepersonstep",
            name="how_to",
        ),
        migrations.DeleteModel(
            name="PersonStepQuestion",
        ),
        migrations.DeleteModel(
            name="ManualStepGuide",
        ),
    ]
