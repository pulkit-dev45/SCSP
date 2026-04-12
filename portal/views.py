import json
import traceback
from datetime import datetime

import openpyxl
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from openpyxl.styles import Font, PatternFill

from .forms import ExcelUploadForm, StudentDataForm
from .models import Dlc, NsqfElectronics, NsqfIT, studentdata

MONTH_MAP = {
    m: i
    for i, m in enumerate(
        [
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ],
        1,
    )
}

SORTABLE_FIELDS = {
    "roll_number",
    "batch_code",
    "name",
    "course_name",
    "father_name",
    "mother_name",
    "dob",
    "gender",
    "address",
    "qualifications",
    "aadhaar",
    "scheme",
    "nsqf",
    "course_hour",
    "course_category",
    "center_name",
    "mode",
    "caste_category",
    "fee",
    "claimable_amount",
    "fee_date",
    "trained",
    "certified",
    "placed",
    "session",
    "claimed",
}

CENTERS = ["inderlok", "janakpuri", "karkardooma"]


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ["true", "yes", "1"]


def parse_date(value):
    if not value:
        return None
    if hasattr(value, "date"):
        return value.date()
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


def quarter_from_date(date_str):
    if not date_str:
        return None, None
    try:
        month_str, year_str = date_str.upper().split("-")[:2]
        month = MONTH_MAP.get(month_str)
        return (f"Q{(month - 1) // 3 + 1}" if month else None), year_str
    except Exception:
        return None, None


def apply_filters(params):
    qs = studentdata.objects.all()

    for field, key in [
        ("center_name", "center"),
        ("mode", "mode"),
        ("caste_category", "caste"),
    ]:
        if params.get(key):
            qs = qs.filter(**{field: params[key]})

    if params.get("session"):
        qs = qs.filter(session__icontains=params["session"])
    if params.get("scheme"):
        qs = qs.filter(scheme__icontains=params["scheme"])

    for key, date_field in [
        ("trained", "trained_date"),
        ("certified", "certified_date"),
    ]:
        if params.get(key):
            fn = qs.filter if params[key] == "true" else qs.exclude
            qs = fn(**{f"{date_field}__gt": ""})

    if params.get("placed"):
        qs = qs.filter(placed=parse_bool(params["placed"]))
    if params.get("claimed"):
        qs = qs.filter(claimed=parse_bool(params["claimed"]))

    if params.get("nsqf") == "yes":
        qs = qs.exclude(nsqf="").exclude(nsqf__isnull=True)
    elif params.get("nsqf") == "no":
        qs = qs.filter(nsqf="") | qs.filter(nsqf__isnull=True)

    quarterly = params.get("quarterly")
    year = params.get("year")

    if quarterly:
        q_part, _, status = quarterly.partition("-")
        # if frontend sends just "Q1" with no status, match either trained or certified
        if not status:

            def match(s):
                qt, _ = quarter_from_date(s.trained_date)
                qc, _ = quarter_from_date(s.certified_date)
                return qt == q_part or qc == q_part
        else:

            def match(s):
                d = s.trained_date if status == "trained" else s.certified_date
                q, y = quarter_from_date(d)
                return q == q_part and (not year or y == year)

        qs = [s for s in qs if match(s)]
    elif year:

        def in_year(s):
            for d in [s.trained_date, s.certified_date]:
                if d and quarter_from_date(d)[1] == year:
                    return True
            return bool(s.session and year in s.session)

        qs = [s for s in qs if in_year(s)]

    sort_field = params.get("sort_field", "name")
    sort_order = params.get("sort_order", "asc")
    if sort_field in SORTABLE_FIELDS and not isinstance(qs, list):
        qs = qs.order_by(f"{'-' if sort_order == 'desc' else ''}{sort_field}")

    return qs


def student_to_dict(s):
    return {
        "id": s.id,
        "roll_number": s.roll_number,
        "batch_code": s.batch_code,
        "name": s.name,
        "father_name": s.father_name,
        "mother_name": s.mother_name,
        "dob": s.dob.strftime("%Y-%m-%d") if s.dob else "",
        "gender": s.gender,
        "address": s.address,
        "qualifications": s.qualifications,
        "aadhaar": s.aadhaar,
        "course_name": s.course_name,
        "scheme": s.scheme,
        "nsqf": s.nsqf,
        "course_hour": s.course_hour,
        "course_category": s.course_category,
        "center_name": s.center_name,
        "mode": s.mode,
        "caste_category": s.caste_category,
        "fee": float(s.fee),
        "claimable_amount": float(s.claimable_amount),
        "fee_date": s.fee_date or "",
        "trained": s.trained,
        "trained_date": s.trained_date,
        "certified": s.certified,
        "certified_date": s.certified_date,
        "placed": s.placed,
        "claimed": s.claimed,
        "session": s.session,
    }


def center_summary(qs):
    summary = {
        "Total": qs.count(),
        "SC": 0,
        "ST": 0,
        "OBC": 0,
        "PWD": 0,
        "GENERAL": 0,
        "B": 0,
        "C": 0,
        "D": 0,
        "E": 0,
    }
    for s in qs:
        if s.caste_category in summary:
            summary[s.caste_category] += 1
        cat = (s.course_category or "").strip().upper()[:1]
        if cat in summary:
            summary[cat] += 1
    return summary


# ─── Auth ────────────────────────────────────────────────────────────────────


def login_view(request):
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST["username"],
            password=request.POST["password"],
        )
        if user:
            login(request, user)
            return redirect("dashboard")
        return render(request, "login.html", {"error": "Invalid credentials"})
    return render(request, "login.html")


def logout_view(request):
    logout(request)
    return redirect("login")


# ─── Dashboard ───────────────────────────────────────────────────────────────


@login_required(login_url="/login")
@ensure_csrf_cookie
def dashboard(request):
    return render(request, "dashboard.html")


# ─── Upload ──────────────────────────────────────────────────────────────────


@login_required(login_url="/login")
def upload(request):
    if request.method != "POST":
        return render(request, "upload.html", {"form": ExcelUploadForm()})

    form = ExcelUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, "upload.html", {"form": form})

    year = form.cleaned_data["year"]
    session = form.cleaned_data["session"]
    session_label = f"{session.upper()[:3]}-{year}"
    success, errors, dupes = 0, 0, 0

    try:
        wb = openpyxl.load_workbook(request.FILES["file"])
        ws = wb.active
        headers = [str(c.value).lower().strip() if c.value else "" for c in ws[1]]
        existing_rolls = set(studentdata.objects.values_list("roll_number", flat=True))

        for row in ws.iter_rows(min_row=2, values_only=True):
            if all(c is None for c in row):
                continue
            try:
                d = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}

                name = str(d.get("name") or "").strip()
                roll = str(d.get("roll_number") or "").strip()
                course_name = str(d.get("course_name") or "").strip()

                if roll in existing_rolls:
                    dupes += 1
                    continue

                try:
                    course_hour = int(float(str(d.get("course_hour") or 0)))
                except Exception:
                    course_hour = 0

                if not name or not course_name or course_hour <= 0:
                    errors += 1
                    continue

                mode = str(d.get("mode") or "offline").lower().strip()
                if mode not in ["offline", "online"]:
                    mode = "offline"

                caste = str(d.get("caste_category") or "GENERAL").upper().strip()
                if caste not in ["OBC", "SC", "ST", "PWD", "GENERAL"]:
                    caste = "GENERAL"

                center = str(d.get("center_name") or "inderlok").lower().strip()
                if center not in CENTERS:
                    center = "inderlok"

                aadhaar_val = d.get("aadhaar")
                aadhaar = (
                    str(int(float(aadhaar_val))).strip()
                    if isinstance(aadhaar_val, (int, float))
                    else str(aadhaar_val or "").strip()
                )

                trained = parse_bool(d.get("trained", False))
                certified = parse_bool(d.get("certified", False))

                studentdata(
                    session=session_label,
                    batch_code=str(d.get("batch_code") or "").strip().upper(),
                    roll_number=roll,
                    name=name,
                    father_name=str(d.get("father_name") or "").strip(),
                    mother_name=str(d.get("mother_name") or "").strip(),
                    dob=parse_date(d.get("dob")),
                    gender=str(d.get("gender") or "Male").strip(),
                    address=str(d.get("address") or "").strip(),
                    qualifications=str(d.get("qualifications") or "").strip(),
                    aadhaar=aadhaar,
                    course_name=course_name,
                    course_hour=course_hour,
                    scheme=str(d.get("scheme") or "").strip(),
                    nsqf=str(d.get("nsqf") or "").strip(),
                    mode=mode,
                    caste_category=caste,
                    center_name=center,
                    fee=float(str(d.get("fee") or 0)),
                    fee_date=parse_date(d.get("fee_date")),
                    trained=trained,
                    trained_date=session_label if trained else "",
                    certified=certified,
                    certified_date=session_label if certified else "",
                    placed=parse_bool(d.get("placed", False)),
                ).save()

                existing_rolls.add(roll)
                success += 1

            except Exception as e:
                errors += 1
                print(traceback.format_exc())

    except Exception as e:
        messages.error(request, f"Error reading file: {e}")
        return redirect("upload")

    messages.success(
        request, f"Uploaded: {success} | Duplicates skipped: {dupes} | Errors: {errors}"
    )
    return redirect("upload")


# ─── Filter ──────────────────────────────────────────────────────────────────


@login_required(login_url="/login")
def filter_students(request):
    students = apply_filters(request.GET)
    page = int(request.GET.get("page", 1))
    limit = int(request.GET.get("limit", 10))
    offset = (page - 1) * limit
    total = len(students) if isinstance(students, list) else students.count()

    return JsonResponse(
        {
            "results": [student_to_dict(s) for s in students[offset : offset + limit]],
            "pagination": {
                "page": page,
                "limit": limit,
                "total_count": total,
                "total_pages": (total + limit - 1) // limit,
                "has_next": page < (total + limit - 1) // limit,
                "has_prev": page > 1,
            },
        }
    )


# ─── Download Filtered Excel ──────────────────────────────────────────────────


@login_required(login_url="/login")
def download_filtered_data(request):
    students = apply_filters(request.GET)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Filtered Students"

    headers = [
        "Roll Number",
        "Batch Code",
        "Name",
        "Father Name",
        "Mother Name",
        "DOB",
        "Gender",
        "Address",
        "Qualifications",
        "Aadhaar",
        "Course Name",
        "Scheme",
        "NSQF",
        "Course Hours",
        "Course Category",
        "Center",
        "Mode",
        "Caste Category",
        "Fee",
        "Claimable Amount",
        "Fee Date",
        "Trained",
        "Trained Date",
        "Certified",
        "Certified Date",
        "Placed",
        "Claimed",
        "Session",
    ]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(
            start_color="CCCCCC", end_color="CCCCCC", fill_type="solid"
        )

    yn = lambda v: "Yes" if v else "No"
    for row, s in enumerate(students, 2):
        for col, val in enumerate(
            [
                s.roll_number,
                s.batch_code,
                s.name,
                s.father_name,
                s.mother_name,
                s.dob.strftime("%Y-%m-%d") if s.dob else "",
                s.gender,
                s.address,
                s.qualifications,
                s.aadhaar,
                s.course_name,
                s.scheme,
                s.nsqf,
                s.course_hour,
                s.course_category,
                s.center_name,
                s.mode,
                s.caste_category,
                float(s.fee),
                float(s.claimable_amount),
                str(s.fee_date) if s.fee_date else "",
                yn(s.trained),
                s.trained_date,
                yn(s.certified),
                s.certified_date,
                yn(s.placed),
                yn(s.claimed),
                s.session,
            ],
            1,
        ):
            ws.cell(row=row, column=col, value=val)

    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(
            max((len(str(c.value)) for c in col if c.value), default=0) + 2, 50
        )

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = "attachment; filename=filtered_students.xlsx"
    wb.save(response)
    return response


# ─── Report ──────────────────────────────────────────────────────────────────


def _session_filter_options():
    sessions = list(
        studentdata.objects.values_list("session", flat=True)
        .distinct()
        .order_by("-session")
    )
    years = sorted({s.split("-")[1] for s in sessions if "-" in s}, reverse=True)
    months = sorted({s.split("-")[0] for s in sessions if "-" in s})
    return years, months


@login_required(login_url="/login")
def download(request):
    p = request.GET
    students = studentdata.objects.all()
    for key in ["year", "session"]:
        if p.get(key):
            students = students.filter(session__icontains=p[key])
    if p.get("center"):
        students = students.filter(center_name=p["center"])

    grouped = {}
    for s in students:
        key = f"{s.course_category}|{s.course_name}|{s.center_name}|{s.session}"
        if key not in grouped:
            grouped[key] = {
                "category": s.course_category,
                "course_name": s.course_name,
                "course_hour": s.course_hour,
                "center_name": s.center_name,
                "session": s.session,
                "scheme": s.scheme,
                "nsqf": s.nsqf,
                **{
                    c: {"trained": 0, "certified": 0, "placed": 0, "total": 0}
                    for c in ["GENERAL", "OBC", "SC", "ST", "PWD"]
                },
            }
        g = grouped[key][s.caste_category]
        g["total"] += 1
        if s.trained_date:
            g["trained"] += 1
        if s.certified_date:
            g["certified"] += 1
        if s.placed:
            g["placed"] += 1

    report_data = list(grouped.values())
    castes = ["GENERAL", "OBC", "SC", "ST", "PWD"]
    totals = {
        c: {"trained": 0, "certified": 0, "placed": 0, "total": 0} for c in castes
    }
    for item in report_data:
        for c in castes:
            for k in totals[c]:
                totals[c][k] += item[c][k]
    totals["grand_total"] = sum(totals[c]["total"] for c in castes)

    years, months = _session_filter_options()
    return render(
        request,
        "download.html",
        {
            "data": report_data,
            "totals": totals,
            "selected_year": p.get("year", ""),
            "selected_session": p.get("session", ""),
            "selected_center": p.get("center", ""),
            "years": years,
            "months": months,
            "centers": CENTERS,
        },
    )


@login_required(login_url="/login")
def api_download_data(request):
    p = request.GET
    students = studentdata.objects.all()
    for key in ["year", "session"]:
        if p.get(key):
            students = students.filter(session__icontains=p[key])
    if p.get("center"):
        students = students.filter(center_name=p["center"])

    return JsonResponse(
        {
            "results": [
                {
                    "course_category": s.course_category,
                    "course_name": s.course_name,
                    "course_hour": s.course_hour,
                    "center_name": s.center_name,
                    "scheme": s.scheme,
                    "nsqf": s.nsqf,
                    "session": s.session,
                    "caste_category": s.caste_category,
                    "trained_date": s.trained_date,
                    "certified_date": s.certified_date,
                    "placed": s.placed,
                    "fee": float(s.fee),
                    "claimable_amount": float(s.claimable_amount),
                }
                for s in students
            ]
        }
    )


# ─── Update ──────────────────────────────────────────────────────────────────


@login_required(login_url="/login")
def update_student(request, student_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        student = studentdata.objects.get(id=student_id)
    except studentdata.DoesNotExist:
        return JsonResponse({"error": "Student not found"}, status=404)

    try:
        body = json.loads(request.body)
        current_month = (
            datetime.now().strftime("%b").upper() + "-" + datetime.now().strftime("%Y")
        )

        str_fields = [
            "name",
            "father_name",
            "mother_name",
            "address",
            "qualifications",
            "aadhaar",
            "course_name",
            "scheme",
            "nsqf",
            "session",
        ]
        for f in str_fields:
            if body.get(f) is not None:
                setattr(student, f, str(body[f]).strip())

        if body.get("batch_code"):
            student.batch_code = str(body["batch_code"]).strip().upper()

        for f in ["dob", "fee_date", "gender", "mode", "caste_category", "center_name"]:
            if body.get(f) is not None:
                setattr(student, f, body[f])

        try:
            student.course_hour = int(
                body.get("course_hour") or student.course_hour or 0
            )
        except (ValueError, TypeError):
            student.course_hour = 0

        try:
            student.fee = float(body.get("fee") or student.fee or 0)
        except (ValueError, TypeError):
            student.fee = 0.0

        if body.get("placed") is not None:
            student.placed = body["placed"]

        for field in ["trained", "certified"]:
            new_val = body.get(field, getattr(student, field))
            date_field = f"{field}_date"
            if new_val and not getattr(student, field):
                setattr(student, date_field, current_month)
            elif not new_val:
                setattr(student, date_field, "")
            setattr(student, field, new_val)

        if body.get("claimed") is not None:
            student.claimed = body["claimed"]

        student.save()
        return JsonResponse(
            {
                "success": True,
                "course_category": student.course_category,
                "claimable_amount": float(student.claimable_amount),
                "trained_date": student.trained_date,
                "certified_date": student.certified_date,
                "claimed": student.claimed,
            }
        )

    except json.JSONDecodeError as e:
        return JsonResponse(
            {"success": False, "error": f"Invalid JSON: {e}"}, status=400
        )
    except Exception as e:
        print(traceback.format_exc())
        return JsonResponse({"success": False, "error": str(e)}, status=400)


# ─── Input ───────────────────────────────────────────────────────────────────


@login_required(login_url="/login")
def inputView(request):
    if request.method == "POST":
        form = StudentDataForm(request.POST)
        if form.is_valid():
            student = form.save(commit=False)
            if student.trained and not student.trained_date:
                student.trained_date = student.session
            if student.certified and not student.certified_date:
                student.certified_date = student.session
            student.save()
            return redirect("dashboard")
    else:
        form = StudentDataForm()

    return render(
        request,
        "input.html",
        {
            "form": form,
            "months": [
                "JAN",
                "FEB",
                "MAR",
                "APR",
                "MAY",
                "JUN",
                "JUL",
                "AUG",
                "SEP",
                "OCT",
                "NOV",
                "DEC",
            ],
            "years": list(range(2020, 2031)),
        },
    )


# ─── Overview ────────────────────────────────────────────────────────────────


def _overview_context(selected_session):
    students = studentdata.objects.all()
    if selected_session:
        students = students.filter(session=selected_session)

    return {
        "all_record": students.count(),
        "centers": [
            {
                "name": n.capitalize(),
                "stats": center_summary(students.filter(center_name=n)),
            }
            for n in CENTERS
        ],
        "sessions": list(
            studentdata.objects.values_list("session", flat=True)
            .distinct()
            .order_by("-session")
        ),
        "selected_session": selected_session,
    }


@login_required(login_url="/login")
def overview(request):
    ctx = _overview_context(request.GET.get("session", ""))
    ctx["centers"] = [(c["name"], c["stats"]) for c in ctx["centers"]]
    return render(request, "overview.html", ctx)


@login_required(login_url="/login")
def overview_data(request):
    return JsonResponse(_overview_context(request.GET.get("session", "")))


# ─── Courses ─────────────────────────────────────────────────────────────────


def courses(request):
    return render(
        request,
        "view_courses.html",
        {
            "It": NsqfIT.objects.all(),
            "elctro": NsqfElectronics.objects.all(),
            "dlc": Dlc.objects.all(),
        },
    )
