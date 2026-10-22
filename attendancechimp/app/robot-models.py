# robot-models.py — LLM-generated data model, for comparison with models.py.
# LLM: Claude (claude.ai)
#
# Prompt:
# Write a Django models.py for an app called AttendanceChimp that tracks class
# attendance using QR codes. Requirements:
# - Users are students and instructors (two roles). Use separate InstructorProfile
#   and StudentProfile models linked to Django's built-in User.
# - Course: has a name, exactly one instructor, and a meeting schedule.
# - Enrollment: links students to courses (many-to-many).
# - Lecture: linked to a course, has a date and a QR code identifier.
# - QRCodeUpload: a photo uploaded by a student, linked to a student and
#   optionally to a lecture.
# Design goals: simple to use, supports attendance analytics, robust to cheating.
# Include docstrings.


"""
Data models for AttendanceChimp: QR-code-based class attendance.

How check-in works
------------------
1. During a lecture, the instructor's screen shows a QR code that encodes a
   short-lived ``QRToken``. A new token is issued every
   ``Lecture.token_lifetime_seconds``, so a photo of the code goes stale quickly.
2. A student photographs the code and uploads it as a ``QRCodeUpload``.
   The server records its own receipt time, a hash of the image, and device
   metadata. It never relies on the student's clock.
3. A verification service (outside this file) decodes the image, matches the
   token, and checks the following:
     * the token exists and was valid when the server received the upload;
     * the lecture's check-in window is open;
     * the student is actively enrolled in the course;
     * the student has no accepted check-in for this lecture yet;
     * the same image (by hash) hasn't been submitted by anyone else.
   It then sets the upload's ``status`` and, on success, writes an
   ``AttendanceRecord``.
4. After the window closes, a job creates ``ABSENT`` records for enrolled
   students with no record, so every (student, lecture) pair has exactly one
   row. Analytics queries become simple counts with no gaps.

Anti-cheating measures in the schema
------------------------------------
* Rotating, short-lived tokens (``QRToken``) instead of one static code.
* Server-side timestamps (``uploaded_at``) and a stored check-in window.
* ``sha256`` and ``perceptual_hash`` on uploads, for spotting shared photos.
* IP, user agent, device ID, and optional geolocation, kept for review.
* Database constraints: one accepted upload and one attendance record per
  student per lecture.
* Every upload is kept, including rejected ones, as an audit trail with
  reasons and reviewer info.

Notes
-----
* ``ImageField`` requires Pillow.
* Conditional ``UniqueConstraint`` is enforced on PostgreSQL and SQLite.
  MySQL ignores the condition, so validate in application code there.
"""

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count, F, Q
from django.utils import timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_qr_token():
    """Return a cryptographically random, URL-safe token for embedding in a QR code."""
    return secrets.token_urlsafe(24)


def qr_upload_path(instance, filename):
    """
    Build the storage path for an uploaded QR photo.

    The original filename is discarded and replaced with a random name. This
    avoids collisions and stops user-controlled names from reaching storage.
    """
    ext = os.path.splitext(filename)[1].lower()[:10]
    return f"qr_uploads/{instance.student_id}/{uuid.uuid4().hex}{ext}"


class TimeStampedModel(models.Model):
    """Abstract base adding ``created_at``/``updated_at`` audit timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

class InstructorProfile(TimeStampedModel):
    """
    Instructor role data, attached one-to-one to a Django ``User``.

    A user is an instructor if ``user.instructor_profile`` exists.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="instructor_profile",
    )
    department = models.CharField(max_length=120, blank=True)
    employee_id = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["user__last_name", "user__first_name"]

    def __str__(self):
        return self.user.get_full_name() or self.user.get_username()

    def clean(self):
        """Keep the two roles separate: a student cannot also be an instructor."""
        if StudentProfile.objects.filter(user_id=self.user_id).exists():
            raise ValidationError("This user already has a student profile.")


class StudentProfile(TimeStampedModel):
    """
    Student role data, attached one-to-one to a Django ``User``.

    A user is a student if ``user.student_profile`` exists.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="student_profile",
    )
    student_number = models.CharField(max_length=40, unique=True)

    class Meta:
        ordering = ["user__last_name", "user__first_name"]

    def __str__(self):
        name = self.user.get_full_name() or self.user.get_username()
        return f"{name} ({self.student_number})"

    def clean(self):
        """Keep the two roles separate: an instructor cannot also be a student."""
        if InstructorProfile.objects.filter(user_id=self.user_id).exists():
            raise ValidationError("This user already has an instructor profile.")


# ---------------------------------------------------------------------------
# Courses and schedule
# ---------------------------------------------------------------------------

class Course(TimeStampedModel):
    """
    A course taught by exactly one instructor for one term.

    The weekly meeting schedule is stored as ``MeetingTime`` rows, not free
    text, so lectures can be generated automatically and analytics can group
    by day and time. Check-in policy settings live here so each course can
    tune them.
    """

    name = models.CharField(max_length=200)
    code = models.CharField(max_length=30, help_text="e.g. CS 101")
    term = models.CharField(max_length=30, help_text="e.g. Fall 2026")
    instructor = models.ForeignKey(
        InstructorProfile,
        on_delete=models.PROTECT,  # don't silently delete courses with an instructor
        related_name="courses",
    )
    students = models.ManyToManyField(
        StudentProfile,
        through="Enrollment",
        related_name="courses",
        blank=True,
    )
    start_date = models.DateField()
    end_date = models.DateField()

    # Check-in policy
    checkin_opens_minutes_before = models.PositiveSmallIntegerField(
        default=5, help_text="Minutes before the lecture starts that check-in opens."
    )
    checkin_closes_minutes_after = models.PositiveSmallIntegerField(
        default=20, help_text="Minutes after the lecture starts that check-in closes."
    )
    late_after_minutes = models.PositiveSmallIntegerField(
        default=10, help_text="Check-ins later than this after start count as LATE."
    )

    class Meta:
        ordering = ["-start_date", "code"]
        constraints = [
            models.UniqueConstraint(fields=["code", "term"], name="unique_course_code_per_term"),
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")),
                name="course_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.code}: {self.name} ({self.term})"

    def active_students(self):
        """Return students with an active enrollment in this course."""
        return self.students.filter(enrollments__course=self, enrollments__status=Enrollment.Status.ACTIVE)

    def is_student_enrolled(self, student):
        """Return True if ``student`` is actively enrolled in this course."""
        return self.enrollments.filter(student=student, status=Enrollment.Status.ACTIVE).exists()


class MeetingTime(models.Model):
    """One recurring weekly meeting slot for a course, e.g. Mondays 10:00–10:50 in Room 204."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="meeting_times")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()
    location = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "weekday", "start_time"], name="unique_meeting_slot"
            ),
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")), name="meeting_end_after_start"
            ),
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.start_time:%H:%M}–{self.end_time:%H:%M}"


class Enrollment(TimeStampedModel):
    """
    Links a student to a course (the many-to-many "through" model).

    Drops are recorded with ``status`` and ``dropped_at`` instead of deleting
    the row, so historical attendance stays explainable.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DROPPED = "dropped", "Dropped"
        AUDIT = "audit", "Auditing"

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="enrollments")
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="enrollments")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    enrolled_at = models.DateTimeField(default=timezone.now)
    dropped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student", "course"], name="unique_enrollment"),
        ]
        indexes = [models.Index(fields=["course", "status"])]

    def __str__(self):
        return f"{self.student} in {self.course.code} [{self.status}]"

    def attendance_summary(self):
        """
        Return this student's attendance counts and rate in this course.

        Example: ``{"present": 20, "late": 3, "absent": 2, "excused": 1,
        "total": 26, "rate": 0.88}``. ``rate`` counts present and late as
        attended and leaves excused lectures out of the denominator.
        """
        counts = dict(
            AttendanceRecord.objects.filter(student=self.student, lecture__course=self.course)
            .values_list("status")
            .annotate(n=Count("id"))
        )
        summary = {s.value: counts.get(s.value, 0) for s in AttendanceRecord.Status}
        summary["total"] = sum(counts.values())
        countable = summary["total"] - summary["excused"]
        attended = summary["present"] + summary["late"]
        summary["rate"] = round(attended / countable, 4) if countable else None
        return summary


# ---------------------------------------------------------------------------
# Lectures and QR codes
# ---------------------------------------------------------------------------

class Lecture(TimeStampedModel):
    """
    A single class meeting of a course on a given date.

    ``qr_code_id`` is the lecture's stable public identifier. The QR code shown
    in class encodes a rotating ``QRToken`` rather than this ID, so a photo
    passed to a friend stops working within seconds.
    """

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="lectures")
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    qr_code_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    topic = models.CharField(max_length=200, blank=True)
    is_cancelled = models.BooleanField(default=False)

    # Check-in window. Filled from the course policy on first save if left blank.
    checkin_opens_at = models.DateTimeField(blank=True, null=True)
    checkin_closes_at = models.DateTimeField(blank=True, null=True)
    token_lifetime_seconds = models.PositiveSmallIntegerField(
        default=30, help_text="How often the displayed QR code rotates."
    )

    class Meta:
        ordering = ["-date", "-start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "date", "start_time"], name="unique_lecture_slot"
            ),
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")), name="lecture_end_after_start"
            ),
        ]
        indexes = [models.Index(fields=["course", "date"])]

    def __str__(self):
        return f"{self.course.code} on {self.date:%Y-%m-%d}"

    @property
    def starts_at(self):
        """The lecture's start as an aware datetime in the current time zone."""
        return timezone.make_aware(datetime.combine(self.date, self.start_time))

    def save(self, *args, **kwargs):
        """Fill in the check-in window from the course policy if it wasn't set explicitly."""
        if self.checkin_opens_at is None:
            self.checkin_opens_at = self.starts_at - timedelta(
                minutes=self.course.checkin_opens_minutes_before
            )
        if self.checkin_closes_at is None:
            self.checkin_closes_at = self.starts_at + timedelta(
                minutes=self.course.checkin_closes_minutes_after
            )
        super().save(*args, **kwargs)

    def is_checkin_open(self, at=None):
        """Return True if check-in is open at ``at`` (defaults to now)."""
        at = at or timezone.now()
        return (
            not self.is_cancelled
            and self.checkin_opens_at <= at <= self.checkin_closes_at
        )

    def current_token(self, now=None):
        """
        Return the token to display right now, issuing a new one if none is valid.

        The instructor's display calls this every few seconds.
        """
        now = now or timezone.now()
        token = (
            self.qr_tokens.filter(valid_from__lte=now, valid_until__gt=now)
            .order_by("-valid_from")
            .first()
        )
        if token is None:
            token = QRToken.objects.create(
                lecture=self,
                valid_from=now,
                valid_until=now + timedelta(seconds=self.token_lifetime_seconds),
            )
        return token

    def attendance_counts(self):
        """Return ``{status: count}`` for this lecture's attendance records."""
        return dict(self.attendance_records.values_list("status").annotate(n=Count("id")))


class QRToken(models.Model):
    """
    A short-lived secret encoded in the QR code shown during a lecture.

    Tokens are kept after they expire, which lets the server tell an expired
    token (likely a forwarded photo) apart from a forged one.
    """

    lecture = models.ForeignKey(Lecture, on_delete=models.CASCADE, related_name="qr_tokens")
    token = models.CharField(max_length=64, unique=True, default=generate_qr_token, editable=False)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()

    class Meta:
        ordering = ["-valid_from"]
        indexes = [models.Index(fields=["lecture", "valid_from"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_until__gt=F("valid_from")), name="token_valid_range"
            ),
        ]

    def __str__(self):
        return f"Token for {self.lecture} ({self.valid_from:%H:%M:%S})"

    def is_valid_at(self, at, grace_seconds=10):
        """
        Return True if the token was valid at ``at``.

        ``grace_seconds`` allows for upload latency on slow networks.
        """
        return self.valid_from <= at <= self.valid_until + timedelta(seconds=grace_seconds)


# ---------------------------------------------------------------------------
# Uploads and attendance
# ---------------------------------------------------------------------------

class QRCodeUpload(models.Model):
    """
    A photo of a lecture's QR code, uploaded by a student as a check-in attempt.

    ``lecture`` is optional because the student only sends a photo. The server
    determines the lecture by decoding the token, so students can't choose it
    themselves. If decoding fails, ``lecture`` stays empty and the upload is
    rejected as unreadable.

    Every attempt is kept, including rejected ones, so instructors can review
    suspicious patterns.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending verification"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        FLAGGED = "flagged", "Flagged for review"

    class RejectionReason(models.TextChoices):
        UNREADABLE = "unreadable", "No QR code could be decoded"
        UNKNOWN_TOKEN = "unknown_token", "QR code not recognised"
        EXPIRED_TOKEN = "expired_token", "QR code had expired"
        OUTSIDE_WINDOW = "outside_window", "Check-in window closed"
        NOT_ENROLLED = "not_enrolled", "Student not enrolled in course"
        DUPLICATE_CHECKIN = "duplicate_checkin", "Already checked in"
        DUPLICATE_IMAGE = "duplicate_image", "Same photo submitted by another student"
        LOCATION_MISMATCH = "location_mismatch", "Location far from classroom"
        MANUAL = "manual", "Rejected by instructor"

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="qr_uploads")
    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qr_uploads",
    )
    image = models.ImageField(upload_to=qr_upload_path)

    # Verification results
    decoded_payload = models.TextField(blank=True, help_text="Raw text decoded from the QR image.")
    matched_token = models.ForeignKey(
        QRToken, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploads"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.CharField(
        max_length=20, choices=RejectionReason.choices, blank=True
    )

    # Forensics (anti-cheating)
    uploaded_at = models.DateTimeField(auto_now_add=True, help_text="Server receipt time (trusted).")
    exif_taken_at = models.DateTimeField(
        null=True, blank=True, help_text="Capture time from photo metadata (untrusted)."
    )
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    perceptual_hash = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="Perceptual hash for spotting resized or recompressed copies of one photo.",
    )
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    device_id = models.CharField(max_length=64, blank=True, db_index=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Manual review
    reviewed_by = models.ForeignKey(
        InstructorProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_uploads"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["lecture", "status"]),
            models.Index(fields=["student", "uploaded_at"]),
        ]
        constraints = [
            # At most one accepted check-in per student per lecture.
            models.UniqueConstraint(
                fields=["student", "lecture"],
                condition=Q(status="accepted"),
                name="one_accepted_upload_per_student_lecture",
            ),
            # An accepted upload must be tied to a lecture.
            models.CheckConstraint(
                condition=~Q(status="accepted") | Q(lecture__isnull=False),
                name="accepted_upload_has_lecture",
            ),
            # A rejection must say why.
            models.CheckConstraint(
                condition=~Q(status="rejected") | ~Q(rejection_reason=""),
                name="rejected_upload_has_reason",
            ),
        ]

    def __str__(self):
        return f"Upload by {self.student} at {self.uploaded_at:%Y-%m-%d %H:%M} [{self.status}]"

    def save(self, *args, **kwargs):
        """Compute the SHA-256 of the image on first save, for duplicate detection."""
        if self.image and not self.sha256:
            hasher = hashlib.sha256()
            for chunk in self.image.chunks():
                hasher.update(chunk)
            self.sha256 = hasher.hexdigest()
        super().save(*args, **kwargs)

    def duplicate_uploads_by_others(self):
        """Return other students' uploads with the same exact or perceptual image hash."""
        q = Q(sha256=self.sha256) if self.sha256 else Q(pk__in=[])
        if self.perceptual_hash:
            q |= Q(perceptual_hash=self.perceptual_hash)
        return QRCodeUpload.objects.filter(q).exclude(student_id=self.student_id)


class AttendanceRecord(TimeStampedModel):
    """
    The final attendance status of one student for one lecture.

    This is the table analytics should query. It holds exactly one row per
    (student, lecture), including absences, so rates are plain counts.
    Records can come from an accepted upload or from a manual instructor
    decision such as an excused absence.
    """

    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        LATE = "late", "Late"
        ABSENT = "absent", "Absent"
        EXCUSED = "excused", "Excused"

    class Source(models.TextChoices):
        QR = "qr", "QR check-in"
        MANUAL = "manual", "Instructor entry"
        AUTO = "auto", "Automatic (no check-in)"

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="attendance_records")
    lecture = models.ForeignKey(Lecture, on_delete=models.CASCADE, related_name="attendance_records")
    status = models.CharField(max_length=10, choices=Status.choices)
    source = models.CharField(max_length=10, choices=Source.choices)
    upload = models.OneToOneField(
        QRCodeUpload,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_record",
    )
    checked_in_at = models.DateTimeField(null=True, blank=True)
    marked_by = models.ForeignKey(
        InstructorProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="marked_attendance"
    )
    note = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student", "lecture"], name="one_record_per_student_lecture"),
        ]
        indexes = [
            models.Index(fields=["lecture", "status"]),
            models.Index(fields=["student", "status"]),
        ]

    def __str__(self):
        return f"{self.student} – {self.lecture}: {self.status}"

    @classmethod
    def status_for_checkin(cls, lecture, checked_in_at):
        """Return PRESENT or LATE for a check-in time, based on the course's late threshold."""
        late_cutoff = lecture.starts_at + timedelta(minutes=lecture.course.late_after_minutes)
        return cls.Status.LATE if checked_in_at > late_cutoff else cls.Status.PRESENT