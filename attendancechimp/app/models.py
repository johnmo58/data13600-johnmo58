"""
models.py — Data model for AttendanceChimp.

AttendanceChimp is an electronic class attendance tool.  Instructors create
courses and lectures; each lecture is assigned a random QR code.  Students
photograph the QR code and upload the image.  The system decodes the QR code,
matches it to a lecture, and records the attendance.

Entities
--------
- InstructorProfile   : extends Django User → instructor-specific data
- StudentProfile  : extends Django User → student-specific data
- Course          : a course taught by one instructor
- Enrollment      : junction table linking students to courses
- Lecture         : a single class meeting (has a QR-code identifier)
- QRCodeUpload    : a student's uploaded QR-code photograph
"""

from django.db import models
from django.contrib.auth.models import User

import logging
import string
import random

logger = logging.getLogger(__name__) # set up tracking, auditing, and debugging
                                                               # allows using logger.warning(), logger.error(), 
                                                               # logger.info(), etc. Better than print() for debuggin... 


# ---------------------------------------------------------------------------
# User profiles
# ---------------------------------------------------------------------------
class InstructorProfile(models.Model):
    """Instructor-specific data, linked one-to-one to a Django User."""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    department = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"Instructor: {self.user.username}"


class StudentProfile(models.Model):
    """Student-specific data, linked one-to-one to a Django User."""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    student_id = models.CharField(max_length=10, unique=True)

    def __str__(self):
        return f"Student: {self.user.username}"

# ---------------------------------------------------------------------------
# Course
# ---------------------------------------------------------------------------
class Course(models.Model):
    """A course taught by exactly one instructor.

    The meeting schedule is stored as the days the class meets
    (e.g. "MWF" or "TR") plus a start and end time.
    """
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20)  # e.g. "DATA 13600"
    instructor = models.ForeignKey(
        InstructorProfile, on_delete=models.PROTECT, related_name="courses"
    )
    meeting_days = models.CharField(max_length=10)  # e.g. "MWF", "TR"
    start_time = models.TimeField()
    end_time = models.TimeField()

    def __str__(self):
        return f"{self.code}: {self.name}"

# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------
class Enrollment(models.Model):
    """Junction table linking students to courses (many-to-many).

    Each row means "this student is enrolled in this course."
    A student cannot be enrolled in the same course twice.
    """
    student = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="enrollments"
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="enrollments"
    )
    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course"], name="unique_enrollment"
            )
        ]

    def __str__(self):
        return f"{self.student} in {self.course}"

# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

def generate_qr_code(length=12):
    """Return a random string of capital letters and digits for a lecture's QR code."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


class Lecture(models.Model):
    """A single class meeting of a course.

    Each lecture gets a unique, randomly generated QR code. Students
    photograph that code to prove they attended this specific lecture.
    """
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="lectures"
    )
    date = models.DateField()
    qr_code = models.CharField(max_length=32, unique=True, default=generate_qr_code)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.course.code} on {self.date}"


# ---------------------------------------------------------------------------
# QR Code Upload
# ---------------------------------------------------------------------------
class QRCodeUpload(models.Model):
    """A photo of a lecture's QR code, uploaded by a student.

    The lecture link is optional: it is filled in after the uploaded
    image is decoded and its code matched to a Lecture's qr_code.
    If the code can't be read or matches no lecture, the upload is
    still kept as a record, with lecture left empty.

    uploaded_at records when the photo was submitted, so attendance
    can be checked against the lecture's date and meeting time.
    """
    student = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="uploads"
    )
    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploads",
    )
    image = models.FileField(upload_to="qr_uploads/")
    decoded_code = models.CharField(max_length=32, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student} upload at {self.uploaded_at}"


# ---------------------------------------------------------------------------
# Helper / API functions
# ---------------------------------------------------------------------------



