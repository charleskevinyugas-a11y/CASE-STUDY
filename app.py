from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory
from flask_sqlalchemy import SQLAlchemy
import datetime
import os
from werkzeug.utils import secure_filename
from collections import deque
import copy

app = Flask(__name__)
app.secret_key = "replace-with-a-secure-random-key"

# --------------------
# QUEUE & STACK SETUP
# --------------------
action_log_queue = deque(maxlen=100)  # Keep last 100 actions
undo_stack = []  # Stack for undo operations
redo_stack = []  # Stack for redo operations

# --------------------
# FILE UPLOAD SETUP
# --------------------
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'pictures')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --------------------
# DATABASE SETUP
# --------------------
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///students.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


# --------------------
# DATABASE MODEL
# --------------------
class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    program = db.Column(db.String(120), nullable=True)  # NEW: program field
    total_hours = db.Column(db.Integer, default=0)
    completed_hours = db.Column(db.Integer, default=0)
    picture = db.Column(db.String(255), nullable=True)  # NEW: picture filename

    # relationship to time entries (cascade deletes so child entries removed when student is deleted)
    time_entries = db.relationship("TimeEntry", backref="student", lazy=True, cascade="all, delete-orphan")

    @property
    def remaining_hours(self):
        return self.total_hours - self.completed_hours

# NEW model: time entries (clock in / clock out)
class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    clock_in = db.Column(db.DateTime, nullable=False)
    clock_out = db.Column(db.DateTime, nullable=True)

    @property
    def duration_hours(self):
        if self.clock_out:
            return (self.clock_out - self.clock_in).total_seconds() / 3600.0
        return None


# NEW model: action log for audit trail (QUEUE implementation)
class ActionLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action_type = db.Column(db.String(50), nullable=False)  # 'ADD', 'EDIT', 'DELETE'
    student_name = db.Column(db.String(120), nullable=False)
    student_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.Text, nullable=True)  # JSON string of changes
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def __repr__(self):
        return f"<ActionLog {self.action_type}: {self.student_name} at {self.timestamp}>"


# --------------------
# HELPER FUNCTIONS FOR QUEUE & STACK
# --------------------
def log_action(action_type, student_name, student_id=None, details=None):
    """Log an action to the database and add to in-memory queue (QUEUE implementation)"""
    import json
    action = ActionLog(
        action_type=action_type,
        student_name=student_name,
        student_id=student_id,
        details=json.dumps(details) if details else None
    )
    db.session.add(action)
    db.session.commit()
    
    # Also add to in-memory queue for fast access
    action_log_queue.append({
        'id': action.id,
        'action_type': action_type,
        'student_name': student_name,
        'student_id': student_id,
        'timestamp': action.timestamp
    })


def save_to_undo_stack(student_id, action_type, student_data):
    """Save current state to undo stack (STACK implementation)"""
    undo_entry = {
        'student_id': student_id,
        'action_type': action_type,
        'data': copy.deepcopy(student_data)
    }
    undo_stack.append(undo_entry)
    redo_stack.clear()  # Clear redo when new action performed


def get_recent_actions(limit=20):
    """Get recent actions from database (QUEUE behavior - FIFO)"""
    return ActionLog.query.order_by(ActionLog.timestamp.desc()).limit(limit).all()


def get_student_snapshot(student):
    """Create a snapshot of student data for undo/redo"""
    return {
        'id': student.id,
        'name': student.name,
        'program': student.program,
        'total_hours': student.total_hours,
        'completed_hours': student.completed_hours,
        'picture': student.picture
    }


# --------------------
# MERGE SORT
# --------------------
def merge_sort(data, key=lambda x: x, reverse=False):
    if len(data) <= 1:
        return data

    mid = len(data) // 2
    left = merge_sort(data[:mid], key, reverse)
    right = merge_sort(data[mid:], key, reverse)

    return merge(left, right, key, reverse)


def merge(left, right, key, reverse):
    result = []
    i = j = 0

    while i < len(left) and j < len(right):
        if reverse:
            if key(left[i]) > key(right[j]):
                result.append(left[i])
                i += 1
            else:
                result.append(right[j])
                j += 1
        else:
            if key(left[i]) < key(right[j]):
                result.append(left[i])
                i += 1
            else:
                result.append(right[j])
                j += 1

    result.extend(left[i:])
    result.extend(right[j:])
    return result


# --------------------
# MANUAL SEARCH ALGORITHMS (no DB LIKE / no bisect)
# --------------------
def linear_search_students(students, q):
    """Case-insensitive substring search over name and program."""
    q = q.lower()
    results = []
    for s in students:
        name = (s.name or "").lower()
        prog = (s.program or "").lower()
        if q in name or q in prog:
            results.append(s)
    return results

def binary_prefix_search_students(students, prefix):
    """
    students must be sorted by student.name.lower() (use merge_sort).
    Returns students whose name starts with prefix (case-insensitive).
    """
    prefix = prefix.lower()
    if prefix == "":
        return students[:]  # all

    # build list of lowercase names
    names = [s.name.lower() for s in students]

    # find leftmost index where name >= prefix
    lo, hi = 0, len(names) - 1
    left = len(names)
    while lo <= hi:
        mid = (lo + hi) // 2
        if names[mid] >= prefix:
            left = mid
            hi = mid - 1
        else:
            lo = mid + 1

    # construct a high-key by appending a high unicode char to prefix
    hi_key = prefix + "\uffff"

    # find rightmost index where name <= hi_key (i.e. first > hi_key)
    lo, hi = 0, len(names) - 1
    right = len(names)
    while lo <= hi:
        mid = (lo + hi) // 2
        if names[mid] > hi_key:
            right = mid
            hi = mid - 1
        else:
            lo = mid + 1

    # filter to ensure startswith (left/right may include non-matching names if same ordering)
    result = []
    for s in students[left:right]:
        if s.name.lower().startswith(prefix):
            result.append(s)
    return result


# --------------------
# ROUTES
# --------------------
@app.route("/pictures/<filename>")
def download_file(filename):
    """Serve uploaded picture files"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin")
def admin_dashboard():
    students_all = Student.query.all()

    # build component/program list for filter dropdown
    programs = sorted({(s.program or "").strip() for s in students_all if s.program and s.program.strip()})

    # status options
    statuses = ["Completed", "In progress"]

    # program & status filters from query string
    selected_program = request.args.get("program_filter", "").strip() or None
    selected_status = request.args.get("status_filter", "").strip() or None

    # start with all students, apply program filter if provided
    if selected_program:
        students = [s for s in students_all if (s.program or "").strip() == selected_program]
    else:
        students = list(students_all)

    # apply status filter if provided
    if selected_status:
        if selected_status == "Completed":
            students = [s for s in students if s.total_hours and s.completed_hours >= s.total_hours]
        elif selected_status == "In progress":
            students = [s for s in students if not (s.total_hours and s.completed_hours >= s.total_hours)]

    sort_by = request.args.get("sort", "name")
    order = request.args.get("order", "asc")
    reverse = (order == "desc")

    # apply merge sort
    if sort_by == "name":
        students = merge_sort(students, key=lambda x: x.name.lower(), reverse=reverse)
    elif sort_by == "program":
        students = merge_sort(students, key=lambda x: (x.program or "").lower(), reverse=reverse)
    elif sort_by == "total_hours":
        students = merge_sort(students, key=lambda x: x.total_hours, reverse=reverse)
    elif sort_by == "completed_hours":
        students = merge_sort(students, key=lambda x: x.completed_hours, reverse=reverse)
    elif sort_by == "remaining_hours":
        students = merge_sort(students, key=lambda x: x.remaining_hours, reverse=reverse)
    elif sort_by == "status":
        # Completed first (0), In progress (1)
        students = merge_sort(students, key=lambda x: 0 if (x.total_hours and x.completed_hours >= x.total_hours) else 1, reverse=reverse)

    return render_template("admin_dashboard.html",
                           students=students,
                           sort=sort_by,
                           order=order,
                           programs=programs,
                           selected_program=selected_program,
                           statuses=statuses,
                           selected_status=selected_status)

@app.route("/add_student", methods=["GET", "POST"])
def add_student():
    if request.method == "POST":
        name = request.form["name"]
        program = request.form.get("program")  # NEW: read program
        total_hours = int(request.form["total_hours"])
        completed_hours = int(request.form.get("completed_hours", 0))
        
        # Handle file upload
        picture_filename = None
        if 'picture' in request.files:
            file = request.files['picture']
            if file and file.filename != '' and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                # Add timestamp to avoid filename conflicts
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_")
                filename = timestamp + filename
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                picture_filename = filename

        new_student = Student(
            name=name,
            program=program,
            total_hours=total_hours,
            completed_hours=completed_hours,
            picture=picture_filename
        )

        db.session.add(new_student)
        db.session.commit()

        # Save to undo stack (STACK implementation)
        student_data = {
            'name': name,
            'program': program,
            'total_hours': total_hours,
            'completed_hours': completed_hours,
            'picture': picture_filename
        }
        save_to_undo_stack(new_student.id, 'ADD', student_data)

        # Log action to queue
        log_action(
            'ADD',
            name,
            new_student.id,
            {
                'program': program,
                'total_hours': total_hours,
                'completed_hours': completed_hours,
                'picture': picture_filename
            }
        )

        flash(f"Student '{name}' has been successfully added!")
        return redirect(url_for("admin_dashboard"))

    return render_template("add_student.html")

# EDIT STUDENT
@app.route("/edit_student/<int:id>", methods=["GET", "POST"])
def edit_student(id):
    student = Student.query.get_or_404(id)

    if request.method == "POST":
        # Save current state to undo stack before modifying
        save_to_undo_stack(id, 'EDIT', get_student_snapshot(student))

        old_name = student.name
        student.name = request.form["name"]
        student.program = request.form.get("program")  # NEW: update program
        student.total_hours = int(request.form["total_hours"])
        student.completed_hours = int(request.form.get("completed_hours", 0))
        
        # Handle file upload
        if 'picture' in request.files:
            file = request.files['picture']
            if file and file.filename != '' and allowed_file(file.filename):
                # Delete old picture if exists
                if student.picture and os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], student.picture)):
                    os.remove(os.path.join(app.config['UPLOAD_FOLDER'], student.picture))
                
                filename = secure_filename(file.filename)
                # Add timestamp to avoid filename conflicts
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_")
                filename = timestamp + filename
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                student.picture = filename

        db.session.commit()

        # Log action to queue
        log_action(
            'EDIT',
            student.name,
            student.id,
            {
                'old_name': old_name,
                'new_name': student.name,
                'program': student.program,
                'total_hours': student.total_hours,
                'completed_hours': student.completed_hours
            }
        )

        flash(f"Student '{student.name}' has been successfully updated!")
        return redirect(url_for("admin_dashboard"))

    return render_template("edit_student.html", student=student)


# DELETE STUDENT
@app.route("/delete_student/<int:id>", methods=["POST"])
def delete_student(id):
    student = Student.query.get_or_404(id)
    student_name = student.name
    
    # Save current state to undo stack before deletion
    save_to_undo_stack(id, 'DELETE', get_student_snapshot(student))
    
    # delete any related time entries first to avoid FK constraint errors
    try:
        TimeEntry.query.filter_by(student_id=student.id).delete()
    except Exception:
        # fallback: iterate and delete
        for te in list(student.time_entries):
            db.session.delete(te)

    # NOTE: Don't delete picture file - keep it for undo/redo restore functionality
    # Picture files can be cleaned up later if needed for students with no remaining references

    db.session.delete(student)
    db.session.commit()
    
    # Log action to queue
    log_action(
        'DELETE',
        student_name,
        id,
        {'deleted': True}
    )
    
    flash(f"Student '{student_name}' has been successfully deleted!")
    return redirect(url_for("admin_dashboard"))


# helper: convert naive UTC datetimes to Philippine Time (PHT) and format
def to_pht(dt):
    if not dt:
        return ""
    # if already a string, return as-is
    if isinstance(dt, str):
        return dt
    try:
        pht = dt + datetime.timedelta(hours=8)
        return pht.strftime("%Y-%m-%d %H:%M:%S PHT")
    except Exception:
        return str(dt)

# register as jinja filter
app.jinja_env.filters["to_pht"] = to_pht


@app.route("/student/<int:id>/clock_in", methods=["POST"])
def clock_in(id):
    student = Student.query.get_or_404(id)
    open_entry = TimeEntry.query.filter_by(student_id=id, clock_out=None).first()
    if open_entry:
        return "Student already clocked in. <br><a href='/admin'>Back to Dashboard</a>"
    now = datetime.datetime.utcnow()
    entry = TimeEntry(student_id=id, clock_in=now)
    db.session.add(entry)
    db.session.commit()
    return f"Clocked in at {to_pht(now)}. <br><a href='/admin'>Back to Dashboard</a>"

@app.route("/student/<int:id>/clock_out", methods=["POST"])
def clock_out(id):
    student = Student.query.get_or_404(id)
    open_entry = TimeEntry.query.filter_by(student_id=id, clock_out=None).order_by(TimeEntry.clock_in.desc()).first()
    if not open_entry:
        return "No active clock-in found. <br><a href='/admin'>Back to Dashboard</a>"
    now = datetime.datetime.utcnow()
    open_entry.clock_out = now
    db.session.commit()
    return f"Clocked out at {to_pht(now)}. <br><a href='/admin'>Back to Dashboard</a>"

@app.route("/student/<int:id>/time_entries")
def student_time_entries(id):
    student = Student.query.get_or_404(id)
    entries = TimeEntry.query.filter_by(student_id=id).order_by(TimeEntry.clock_in.desc()).all()
    total_hours = sum((e.duration_hours or 0) for e in entries)
    return render_template("time_entries.html", student=student, entries=entries, total_hours=total_hours)


# ---------- NEW: Student login and student dashboard ----------
@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        sid = request.form.get("student_id")
        if not sid:
            flash("Please select a student.")
            return redirect(url_for("student_login"))
        student = Student.query.get(sid)
        if not student:
            flash("Student not found.")
            return redirect(url_for("student_login"))
        session["student_id"] = student.id
        return redirect(url_for("student_dashboard"))

    students = Student.query.order_by(Student.name).all()
    return render_template("student_login.html", students=students)


@app.route("/student_dashboard")
def student_dashboard():
    sid = session.get("student_id")
    if not sid:
        return redirect(url_for("student_login"))
    student = Student.query.get_or_404(sid)
    open_entry = TimeEntry.query.filter_by(student_id=sid, clock_out=None).first()
    return render_template("student_dashboard.html", student=student, open_entry=open_entry)


@app.route("/student/clock_in", methods=["POST"])
def student_clock_in():
    sid = session.get("student_id")
    if not sid:
        return redirect(url_for("student_login"))

    # load student to check status before allowing clock-in
    student = Student.query.get(sid)
    if not student:
        flash("Student record not found.")
        return redirect(url_for("student_login"))

    # prevent clock-in if student already completed required hours
    if student.total_hours and student.completed_hours >= student.total_hours:
        flash("You have already completed the required hours. Clock-in is not allowed.")
        return redirect(url_for("student_dashboard"))

    open_entry = TimeEntry.query.filter_by(student_id=sid, clock_out=None).first()
    if open_entry:
        flash("You are already clocked in.")
        return redirect(url_for("student_dashboard"))
    now = datetime.datetime.utcnow()
    entry = TimeEntry(student_id=sid, clock_in=now)
    db.session.add(entry)
    db.session.commit()
    flash(f"Clocked in at {to_pht(now)}")
    return redirect(url_for("student_dashboard"))


@app.route("/student/clock_out", methods=["POST"])
def student_clock_out():
    sid = session.get("student_id")
    if not sid:
        return redirect(url_for("student_login"))
    open_entry = TimeEntry.query.filter_by(student_id=sid, clock_out=None).order_by(TimeEntry.clock_in.desc()).first()
    if not open_entry:
        flash("No active clock-in found.")
        return redirect(url_for("student_dashboard"))
    now = datetime.datetime.utcnow()
    open_entry.clock_out = now
    # optional: update student's completed_hours (rounded to nearest hour)
    duration_hours = (open_entry.clock_out - open_entry.clock_in).total_seconds() / 3600.0
    student = Student.query.get(sid)
    if duration_hours:
        student.completed_hours = (student.completed_hours or 0) + round(duration_hours)
    db.session.commit()
    flash(f"Clocked out at {to_pht(now)}")
    return redirect(url_for("student_dashboard"))


@app.route("/student/logout")
def student_logout():
    session.pop("student_id", None)
    return redirect(url_for("student_login"))


# --------------------
# SEARCH ROUTE
# --------------------
@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return redirect(url_for("admin_dashboard"))

    alg = request.args.get("alg", "linear").lower()  # 'linear' or 'binary'
    # fetch full list once
    students = Student.query.all()

    if alg == "binary":
        # sort by name using existing merge_sort, then binary prefix search
        students_sorted = merge_sort(students, key=lambda x: x.name.lower())
        results = binary_prefix_search_students(students_sorted, q)
    else:
        # default: linear substring search (works on name and program)
        results = linear_search_students(students, q)

    # if no results, flash message and redirect back to admin
    if not results:
        flash(f"No students found matching '{q}'", "warning")
        return redirect(url_for("admin_dashboard"))

    # reuse admin template; pass None for sort/order so headers don't toggle unexpectedly
    return render_template("admin_dashboard.html", students=results, sort=None, order=None, programs=[], statuses=[])


# --------------------
# UNDO/REDO ROUTES (STACK implementation)
# --------------------
@app.route("/undo", methods=["POST"])
def undo():
    """Undo the last modification (pop from undo stack)"""
    if not undo_stack:
        flash("Nothing to undo.", "warning")
        return redirect(url_for("admin_dashboard"))
    
    undo_entry = undo_stack.pop()
    student_id = undo_entry['student_id']
    action_type = undo_entry['action_type']
    data = undo_entry['data']
    
    try:
        if action_type == 'EDIT':
            # Restore previous values
            student = Student.query.get(student_id)
            if student:
                student.name = data['name']
                student.program = data['program']
                student.total_hours = data['total_hours']
                student.completed_hours = data['completed_hours']
                student.picture = data['picture']
                db.session.commit()
                flash(f"Undo successful: Restored '{data['name']}'", "success")
                
                # Log undo action
                log_action('UNDO', data['name'], student_id, {'original_action': 'EDIT'})
        
        elif action_type == 'ADD':
            # Delete the student that was added (undo of ADD means delete)
            student = Student.query.get(student_id)
            if student:
                # NOTE: Don't delete picture file - keep it for undo/redo restore functionality
                # Delete time entries
                TimeEntry.query.filter_by(student_id=student_id).delete()
                db.session.delete(student)
                db.session.commit()
                flash(f"Undo successful: Deleted '{student.name}'", "success")
                
                # Log undo action
                log_action('UNDO', student.name, student_id, {'original_action': 'ADD'})
                
                # Push to redo stack so user can redo the add
                redo_stack.append(undo_entry)
            else:
                flash(f"Undo failed: Student not found", "danger")
                undo_stack.append(undo_entry)  # Push back if failed
        
        elif action_type == 'DELETE':
            # Restore the deleted student
            existing = Student.query.get(student_id)
            if existing:
                # Student already exists, just update it to be safe
                existing.name = data['name']
                existing.program = data['program']
                existing.total_hours = data['total_hours']
                existing.completed_hours = data['completed_hours']
                existing.picture = data['picture']
                db.session.commit()
            else:
                # Student doesn't exist, create new
                new_student = Student(
                    id=student_id,
                    name=data['name'],
                    program=data['program'],
                    total_hours=data['total_hours'],
                    completed_hours=data['completed_hours'],
                    picture=data['picture']
                )
                db.session.add(new_student)
                db.session.commit()
            
            flash(f"Undo successful: Restored '{data['name']}'", "success")
            
            # Log undo action
            log_action('UNDO', data['name'], student_id, {'original_action': 'DELETE'})
            
            # Push to redo stack
            redo_stack.append(undo_entry)
    
    except Exception as e:
        flash(f"Undo failed: {str(e)}", "danger")
        undo_stack.append(undo_entry)  # Push back if failed
    
    return redirect(url_for("admin_dashboard"))


@app.route("/redo", methods=["POST"])
def redo():
    """Redo the last undone action (pop from redo stack)"""
    if not redo_stack:
        flash("Nothing to redo.", "warning")
        return redirect(url_for("admin_dashboard"))
    
    redo_entry = redo_stack.pop()
    student_id = redo_entry['student_id']
    action_type = redo_entry['action_type']
    data = redo_entry['data']
    
    try:
        if action_type == 'DELETE':
            # Re-delete the student
            student = Student.query.get(student_id)
            if student:
                TimeEntry.query.filter_by(student_id=student_id).delete()
                # NOTE: Don't delete picture file - keep it for undo/redo restore functionality
                db.session.delete(student)
                db.session.commit()
                flash(f"Redo successful: Deleted '{data['name']}'", "success")
                
                # Log redo action
                log_action('REDO', data['name'], student_id, {'original_action': 'DELETE'})
                
                # Push back to undo stack so it can be undone again
                undo_stack.append(redo_entry)
        
        elif action_type == 'EDIT':
            # Re-apply the edit
            student = Student.query.get(student_id)
            if student:
                student.name = data['name']
                student.program = data['program']
                student.total_hours = data['total_hours']
                student.completed_hours = data['completed_hours']
                student.picture = data['picture']
                db.session.commit()
                flash(f"Redo successful: Updated '{data['name']}'", "success")
                
                # Log redo action
                log_action('REDO', data['name'], student_id, {'original_action': 'EDIT'})
                
                # Push back to undo stack so it can be undone again
                undo_stack.append(redo_entry)
        
        elif action_type == 'ADD':
            # Re-add the student (redo of delete-via-undo means add back)
            existing = Student.query.get(student_id)
            if existing:
                # Student already exists, just update it
                existing.name = data['name']
                existing.program = data['program']
                existing.total_hours = data['total_hours']
                existing.completed_hours = data['completed_hours']
                existing.picture = data['picture']
                db.session.commit()
            else:
                # Student doesn't exist, create new
                new_student = Student(
                    id=student_id,
                    name=data['name'],
                    program=data['program'],
                    total_hours=data['total_hours'],
                    completed_hours=data['completed_hours'],
                    picture=data['picture']
                )
                db.session.add(new_student)
                db.session.commit()
            
            flash(f"Redo successful: Added '{data['name']}'", "success")
            
            # Log redo action
            log_action('REDO', data['name'], student_id, {'original_action': 'ADD'})
            
            # Push back to undo stack
            undo_stack.append(redo_entry)
    
    except Exception as e:
        flash(f"Redo failed: {str(e)}", "danger")
        redo_stack.append(redo_entry)  # Push back if failed
    
    return redirect(url_for("admin_dashboard"))


# --------------------
# ACTIVITY LOG ROUTES
# --------------------
@app.route("/activity_log")
def activity_log():
    """Display recent admin activity (QUEUE behavior - shows in chronological order)"""
    page = request.args.get('page', 1, type=int)
    actions = ActionLog.query.order_by(ActionLog.timestamp.desc()).paginate(page=page, per_page=30)
    
    return render_template("activity_log.html", 
                          actions=actions.items,
                          has_next=actions.has_next,
                          has_prev=actions.has_prev,
                          page=page,
                          undo_available=len(undo_stack) > 0,
                          redo_available=len(redo_stack) > 0)


# --------------------
# RUN THE APP
# --------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
