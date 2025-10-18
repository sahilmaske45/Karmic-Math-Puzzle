# ==============================================================================
# IMPORTS
# ==============================================================================
# --- Flask Core & Extensions ---
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_session import Session

# --- Standard Libraries ---
import os
from datetime import datetime
from collections import defaultdict, OrderedDict
import io
import tempfile 

# --- Third-party Libraries ---
from werkzeug.security import generate_password_hash, check_password_hash
from fpdf import FPDF
import matplotlib

# Configure Matplotlib for a non-GUI environment (crucial for servers)
matplotlib.use('Agg') 
import matplotlib.pyplot as plt

# --- Local Imports ---
import puzzles

# ==============================================================================
# APP & DATABASE CONFIGURATION
# ==============================================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'a_very_secret_key_that_is_long_and_secure'

# --- Database Configuration ---
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- Server-Side Session Configuration ---
# This stores session data on the server's filesystem instead of in browser cookies
# to prevent "cookie too large" errors.
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
session_dir = os.path.join(basedir, 'flask_session')
os.makedirs(session_dir, exist_ok=True)
app.config['SESSION_FILE_DIR'] = session_dir

# --- Initialize Extensions ---
db = SQLAlchemy(app)
server_session = Session(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login' # Redirect to '/login' if user is not authenticated

# ==============================================================================
# DATABASE MODELS
# ==============================================================================
class User(UserMixin, db.Model):
    """Represents a user account."""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(150), nullable=False)
    # The 'cascade' option ensures that if a user is deleted, all their rounds are also deleted.
    rounds = db.relationship('Round', backref='user', lazy=True, cascade="all, delete-orphan")

class Round(db.Model):
    """Represents a single quiz round played by a user."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    correct_answers = db.Column(db.Integer, nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    date_played = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # The 'cascade' option ensures that if a round is deleted, all its answers are also deleted.
    answers = db.relationship('Answer', backref='round', lazy=True, cascade="all, delete-orphan")

class Answer(db.Model):
    """Represents a single answer within a round for persistent report generation."""
    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey('round.id'), nullable=False)
    puzzle_id = db.Column(db.String(50), nullable=False)
    user_answer = db.Column(db.String(200), nullable=False)
    is_correct = db.Column(db.Boolean, nullable=False)

# ==============================================================================
# USER AUTHENTICATION
# ==============================================================================
@login_manager.user_loader
def load_user(user_id):
    """Flask-Login helper function to load a user from the database."""
    return User.query.get(int(user_id))

# ==============================================================================
# MAIN & AUTHENTICATION ROUTES
# ==============================================================================
@app.route('/')
def index():
    """Homepage: Shows welcome message and login/signup options."""
    return render_template("index.html")

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """User registration page."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user:
            flash('Username already exists.', 'danger')
            return redirect(url_for('signup'))

        new_user = User(username=username, password_hash=generate_password_hash(password, method='pbkdf2:sha256'))
        db.session.add(new_user)
        db.session.commit()
        
        login_user(new_user)
        return redirect(url_for('dashboard'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login page."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash('Please check your login details and try again.', 'danger')
            return redirect(url_for('login'))
        
        login_user(user)
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """Logs the current user out."""
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Displays user statistics and round history."""
    user_rounds = Round.query.filter_by(user_id=current_user.id).order_by(Round.date_played.desc()).all()
    total_karma = sum(r.score for r in user_rounds)
    total_correct = sum(r.correct_answers for r in user_rounds)
    total_qs_played = sum(r.total_questions for r in user_rounds)
    overall_accuracy = (total_correct / total_qs_played) * 100 if total_qs_played > 0 else 0
    
    return render_template('dashboard.html', user=current_user, rounds=user_rounds, total_karma=total_karma, overall_accuracy=overall_accuracy)

# ==============================================================================
# QUIZ ROUTES
# ==============================================================================
@app.route('/start_quiz')
def start_quiz():
    """Displays the difficulty selection page."""
    return render_template("select_difficulty.html")

@app.route('/create_quiz/<string:difficulty>')
def create_quiz(difficulty):
    """Generates a quiz set based on difficulty and initializes the session."""
    if difficulty not in ['basic', 'intermediate', 'advanced']:
        flash("Invalid difficulty level selected.", "danger")
        return redirect(url_for('start_quiz'))

    quiz_set = puzzles.generate_quiz_set(difficulty)
    
    if not quiz_set or len(quiz_set) < 25:
        flash(f"Not enough puzzles for the '{difficulty}' level yet. Please select another.", "danger")
        return redirect(url_for('start_quiz'))

    session['quiz_set'] = quiz_set
    session['current_round'] = 0
    session['round_answers'] = []
    
    return redirect(url_for('ask_question'))

@app.route('/question')
def ask_question():
    """Displays the current question in the quiz."""
    if 'quiz_set' not in session:
        return redirect(url_for('start_quiz'))
        
    current_round = session.get('current_round', 0)
    quiz_set = session.get('quiz_set', [])
    
    if current_round >= len(quiz_set):
        return redirect(url_for('final_result'))
        
    puzzle = quiz_set[current_round]
    return render_template('puzzle.html', puzzle=puzzle, question_number=current_round + 1, total_questions=len(quiz_set))

@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    """Processes a user's answer and moves to the next question."""
    user_answer = request.form.get('answer')
    puzzle_id = request.form.get('puzzle_id')
    
    answers = session.get('round_answers', [])
    answers.append({'puzzle_id': puzzle_id, 'user_answer': user_answer})
    session['round_answers'] = answers
    
    session['current_round'] = session.get('current_round', 0) + 1
    
    return redirect(url_for('ask_question'))

@app.route('/result')
def final_result():
    """Calculates and displays the final results, and saves them to the database."""
    if 'round_answers' not in session:
        return redirect(url_for('index'))

    results = []
    final_score = 0
    correct_count = 0
    quiz_set = session.get('quiz_set', [])
    chakra_tally = defaultdict(int)
    ethical_tally = defaultdict(int)

    # Process each answer from the session
    for ans in session.get('round_answers', []):
        puzzle, _ = puzzles.find_puzzle_by_id(ans['puzzle_id'])
        if puzzle:
            is_correct = (str(ans['user_answer']).lower() == str(puzzle['answer']).lower())
            score_change = 10 if is_correct else -5
            final_score += score_change
            if is_correct:
                correct_count += 1
                chakra_tally[puzzle.get('chakra', 'General')] += 1
                ethical_tally[puzzle.get('ethical_category', 'General')] += 1
            results.append({'puzzle': puzzle, 'user_answer': ans['user_answer'], 'is_correct': is_correct, 'score_change': score_change})

    total_questions = len(quiz_set)
    accuracy = (correct_count / total_questions) * 100 if total_questions > 0 else 0
    personality = puzzles.get_karma_personality(final_score, total_questions)
    chakra_alignment = max(chakra_tally, key=chakra_tally.get) if chakra_tally else "N/A"
    ethical_focus = max(ethical_tally, key=ethical_tally.get) if ethical_tally else "N/A"

    # Save results to the database for authenticated users
    if current_user.is_authenticated:
        new_round = Round(user_id=current_user.id, score=final_score, correct_answers=correct_count, total_questions=total_questions)
        db.session.add(new_round)
        db.session.flush() # Flush to get the new_round.id for the Answer objects

        for res in results:
            new_answer = Answer(
                round_id=new_round.id,
                puzzle_id=res['puzzle']['id'],
                user_answer=res['user_answer'],
                is_correct=res['is_correct']
            )
            db.session.add(new_answer)
        db.session.commit()

    # Store results in session for the immediate download button
    session['last_results_for_download'] = results
    
    return render_template('final_result.html', results=results, final_score=final_score, personality=personality, accuracy=accuracy, chakra_alignment=chakra_alignment, ethical_focus=ethical_focus)

# ==============================================================================
# PDF REPORT GENERATION
# ==============================================================================

# --- Matplotlib Helper Functions ---
def create_circular_gauge(accuracy, title):
    """Generates a circular gauge graph and saves it as a temporary PNG file."""
    fig, ax = plt.subplots(figsize=(3, 3), subplot_kw={'aspect': 'equal'})
    color = '#28a745' if accuracy >= 70 else '#ffc107' if accuracy >= 40 else '#dc3545'
    
    ax.pie([100], radius=1.0, colors=['#ededed'])
    ax.pie([accuracy, 100 - accuracy], radius=1.0, colors=[color, 'none'], startangle=90, counterclock=False)
    ax.add_artist(plt.Circle((0, 0), 0.7, color='white'))
    ax.text(0, 0, f"{accuracy:.0f}%", ha='center', va='center', fontsize=28, fontweight='bold', color='#333')
    ax.set_title(title, fontsize=14, pad=15)
    
    # Create a secure temporary file and save the plot to it
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
    plt.savefig(temp_file.name, format='png', bbox_inches='tight', transparent=True)
    plt.close(fig)
    return temp_file.name

def create_bar_chart(category_stats):
    """Generates a bar chart graph and saves it as a temporary PNG file."""
    labels = [name.replace('_', ' ').title() for name in category_stats.keys()]
    accuracies = [stats['accuracy'] for stats in category_stats.values()]
    
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(labels, accuracies, color='#8fd3f4')
    
    ax.set_ylabel('Accuracy (%)', fontsize=12)
    ax.set_title('Performance by Category', fontsize=16, pad=20)
    ax.set_ylim(0, 100)
    ax.tick_params(axis='x', rotation=15, labelsize=10)
    
    for bar in bars:
        yval = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2.0, yval + 2, f'{yval:.0f}%', ha='center', va='bottom')

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
    plt.savefig(temp_file.name, format='png', bbox_inches='tight')
    plt.close(fig)
    return temp_file.name

# --- FPDF PDF Custom Class ---
class PDF(FPDF):
    """Custom PDF class with a predefined header and footer."""
    def header(self):
        self.set_font('Arial', 'B', 16)
        self.cell(0, 10, 'Karmic Puzzle Challenge - Detailed Report', 0, 1, 'C')
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

    def chapter_title(self, title):
        self.set_font('Arial', 'B', 14)
        self.cell(0, 10, title, 0, 1, 'L')
        self.ln(5)

# --- PDF Generation & Download Routes ---
def generate_pdf_response(results):
    """A generic function to generate a PDF from a list of result dictionaries."""
    temp_files = []
    try:
        # 1. Process Data to get overall and category-specific stats
        total_correct = sum(1 for r in results if r['is_correct'])
        total_questions = len(results)
        overall_accuracy = (total_correct / total_questions) * 100 if total_questions > 0 else 0

        category_stats = OrderedDict([
            ("ethics_philosophy", {'correct': 0, 'total': 0}), ("mathematics", {'correct': 0, 'total': 0}),
            ("cultural_knowledge", {'correct': 0, 'total': 0}), ("dharmic_principles", {'correct': 0, 'total': 0}),
            ("history", {'correct': 0, 'total': 0})
        ])
        for result in results:
            puzzle, category_name = puzzles.find_puzzle_by_id(result['puzzle']['id'])
            if category_name in category_stats:
                category_stats[category_name]['total'] += 1
                if result['is_correct']:
                    category_stats[category_name]['correct'] += 1
        
        for cat in category_stats:
            category_stats[cat]['accuracy'] = (category_stats[cat]['correct'] / category_stats[cat]['total']) * 100 if category_stats[cat]['total'] > 0 else 0
        
        # 2. Generate all graph images and store their temporary filenames
        overall_gauge_file = create_circular_gauge(overall_accuracy, "Overall Performance")
        temp_files.append(overall_gauge_file)
        category_gauge_files = [create_circular_gauge(stats['accuracy'], name.replace('_', ' ').title()) for name, stats in category_stats.items()]
        temp_files.extend(category_gauge_files)
        bar_chart_file = create_bar_chart(category_stats)
        temp_files.append(bar_chart_file)

        # 3. Assemble the PDF document
        pdf = PDF()
        
        # --- Summary Page ---
        pdf.add_page()
        pdf.chapter_title('Performance Summary')
        pdf.image(overall_gauge_file, x=75, w=60)
        pdf.ln(10)
        pdf.image(category_gauge_files[0], x=20, y=120, w=50)
        pdf.image(category_gauge_files[1], x=80, y=120, w=50)
        pdf.image(category_gauge_files[2], x=140, y=120, w=50)
        pdf.ln(65)
        pdf.image(category_gauge_files[3], x=50, y=175, w=50)
        pdf.image(category_gauge_files[4], x=110, y=175, w=50)

        # --- Bar Chart Page ---
        pdf.add_page()
        pdf.chapter_title('Category Breakdown')
        pdf.image(bar_chart_file, x=10, w=190)
        pdf.ln(10)
        
        # --- Detailed Review Pages ---
        pdf.add_page()
        pdf.chapter_title('Detailed Question Review')
        for i, result in enumerate(results):
            if pdf.get_y() > 220: # Add a new page if content gets too low
                pdf.add_page()
                pdf.chapter_title('Detailed Question Review (cont.)')
                
            puzzle = result['puzzle']
            # Using multi_cell for text that might wrap, and encoding for compatibility
            pdf.set_font('Arial', 'B', 11)
            pdf.multi_cell(0, 7, f"Q{i+1}: {puzzle['question']}".encode('latin-1', 'replace').decode('latin-1'))
            
            pdf.set_font('Arial', '', 10)
            status = "Correct" if result['is_correct'] else "Incorrect"
            pdf.multi_cell(0, 6, f"Your Answer: {result['user_answer']} ({status})".encode('latin-1', 'replace').decode('latin-1'))
            
            if not result['is_correct']:
                pdf.set_font('Arial', 'I', 10)
                pdf.multi_cell(0, 6, f"Correct Answer: {puzzle['answer']}".encode('latin-1', 'replace').decode('latin-1'))
            
            # Analysis Box
            pdf.set_fill_color(245, 245, 245)
            pdf.set_font('Arial', '', 9)
            analysis_text = (f"Principle: {puzzle['analysis']['principle']}\n"
                           f"Implication: {puzzle['analysis']['implication']}")
            pdf.multi_cell(0, 5, analysis_text.encode('latin-1', 'replace').decode('latin-1'), border=1, fill=True)
            pdf.ln(8)

        # 4. Return the generated PDF as a response
        return Response(
            pdf.output(dest='S').encode('latin-1'),
            mimetype='application/pdf',
            headers={'Content-Disposition': 'attachment;filename=Karmic_Report_Detailed.pdf'}
        )
    finally:
        # 5. Clean up: Securely delete all temporary image files
        for f in temp_files:
            try:
                os.remove(f)
            except OSError as e:
                app.logger.error(f"Error removing temporary file {f}: {e}")

@app.route('/download_round_report/<int:round_id>')
@login_required
def download_round_report(round_id):
    """Downloads a PDF report for a specific, previously played round."""
    round_to_download = Round.query.filter_by(id=round_id, user_id=current_user.id).first_or_404()
    
    # Reconstruct the 'results' list from the database
    results_from_db = []
    for answer in round_to_download.answers:
        puzzle, _ = puzzles.find_puzzle_by_id(answer.puzzle_id)
        if puzzle:
            results_from_db.append({
                'puzzle': puzzle,
                'user_answer': answer.user_answer,
                'is_correct': answer.is_correct
            })
            
    return generate_pdf_response(results_from_db)

@app.route('/download_report')
def download_report():
    """Downloads a PDF report for the most recently completed round."""
    results = session.get('last_results_for_download')
    if not results:
        flash("No recent quiz results found to download.", "danger")
        return redirect(url_for('dashboard'))
        
    return generate_pdf_response(results)

# ==============================================================================
# SCRIPT EXECUTION
# ==============================================================================
if __name__ == '__main__':
    app.run(debug=True)

