import os
from flask import Flask, request, render_template
import processor  # local module for PDF parsing and reporting
from config import BRANDING_PROFILES, DEFAULT_TEAM

# Initialize the Flask app
app = Flask(__name__)

# Configuration: where to save uploads and max file size (16 MB)
app.config['UPLOAD_FOLDER'] = '/tmp'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

@app.context_processor
def inject_branding():
    selected = (
        request.args.get('team')
        or request.args.get('brand')
        or DEFAULT_TEAM
    ).upper()
    branding = BRANDING_PROFILES.get(selected, BRANDING_PROFILES[DEFAULT_TEAM])
    return dict(branding=branding)

def exam_type_from_designator(designator):
    """
    Given a question designator like 'T6B02', return the exam type.

    Args:
        designator (str): Question designator (first letter T/G/E)

    Returns:
        str: 'Technician', 'General', 'Amateur Extra', or 'Unknown'
    """
    exam_map = {'T': 'Technician', 'G': 'General', 'E': 'Amateur Extra'}
    return exam_map.get(designator[0].upper(), 'Unknown')

@app.route('/', methods=['GET', 'POST'])
def index():
    """
    Main route to handle file upload and results display.

    - GET: Render the upload page.
    - POST: Handle uploaded PDF, parse results, and display report.
    """
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file or file.filename == '':
            # No file provided
            return render_template('upload.html', error='Please select a file')

        if not file.filename.lower().endswith('.pdf'):
            # Invalid file type
            return render_template('upload.html', error='Only PDF files are allowed')

        # Save the uploaded file to a temp location
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], 'uploaded.pdf')
        file.save(temp_path)

        try:
            # Parse the PDF to extract all results
            applicant_name, results = processor.parse_exam_pdf(temp_path)

            if not applicant_name or not results:
                return render_template(
                    'upload.html',
                    error="We could not extract any results from this file. "
                          "Please make sure you are uploading an ExamTools 'Results' PDF."
                )

            return render_template(
                'report.html',
                applicant=applicant_name,
                results=results
            )

        except Exception as e:
            # Log error and show friendly message
            print(f"[ERROR] Failed to process PDF: {e}")
            return render_template(
                'upload.html',
                error="We were not able to read the results. Please ensure that you have uploaded "
                      "a valid 'Results' PDF from ExamTools."
            )

        finally:
            # Ensure the uploaded temp file is always deleted
            if os.path.exists(temp_path):
                os.remove(temp_path)

    # GET request: just render the upload page
    return render_template('upload.html')
