# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from unittest.mock import patch

from jobserv.models import (
    db,
    Build,
    BuildStatus,
    Project,
    Run,
)

from tests import JobServTest

from jobserv.notify import _get_build_stats, notify_build_complete_email


class SendmailTest(JobServTest):
    def setUp(self):
        super().setUp()
        self.create_projects("job-1")
        self.proj = Project.query.filter_by(name="job-1").first_or_404()
        self.build = Build.create(self.proj)

        self.run = Run(self.build, "run-name")
        db.session.add(self.run)
        self.run.set_status(BuildStatus.FAILED)

    @patch("jobserv.notify.smtplib")
    def test_notify_build_complete(self, smtplib):
        smtplib.SMTP().starttls.return_value = (220, b"ok")
        smtplib.SMTP().login.return_value = (235, b"ok")
        notify_build_complete_email(self.build, "f@f.com")
        msg = smtplib.SMTP().send_message.call_args_list[0][0][0]
        self.assertEqual("jobserv: job-1 build #1 : FAILED", msg["Subject"])
        self.assertIn("\n  run-name: FAILED", msg.get_payload())

    def test_get_build_stats(self):
        # we already have one build created from constructor
        for i in range(9):
            b = Build.create(self.proj)
            r = Run(b, "run-name")
            db.session.add(r)
            if i % 2:
                r.set_status(BuildStatus.FAILED)
            else:
                r.set_status(BuildStatus.PASSED)
        db.session.commit()
        stats = _get_build_stats(b)
        self.assertEqual(10, stats["total"])
        self.assertEqual(5, stats["passes"])
        self.assertEqual(50, stats["pass_rate"])
