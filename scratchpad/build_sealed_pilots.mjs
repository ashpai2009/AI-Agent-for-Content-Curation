import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputRoot = "/Users/ashmit/Documents/code/Projects/AI-Agent-for-Content-Curation/outputs/sealed-demo-benchmark-20260904";
const workbookDir = `${outputRoot}/workbooks`;
const keyDir = `${outputRoot}/sealed-keys`;
const previewDir = `${outputRoot}/previews`;

const HEADERS = [
  "Problem Name", "Row Type", "Title", "Body Text", "Answer", "answerType",
  "HintID", "Dependency", "mcChoices", "Images (space delimited)", "Parent",
  "OER src", "openstax KC", "KC", "Taxonomy", "License",
];

const COL = {
  problemName: 0, rowType: 1, title: 2, body: 3, answer: 4, answerType: 5,
  hintId: 6, dependency: 7, choices: 8, images: 9, parent: 10, oer: 11,
  openstaxKc: 12, kc: 13, taxonomy: 14, license: 15,
};

function row(values = {}) {
  const result = Array(16).fill(null);
  for (const [key, value] of Object.entries(values)) result[COL[key]] = value;
  return result;
}

function problem(name, title, body, source, kc) {
  return row({ problemName: name, rowType: "problem", title, body, oer: source,
    openstaxKc: kc, kc, taxonomy: "openstax",
    license: "https://creativecommons.org/licenses/by/4.0/ <CC BY 4.0>" });
}

function step(name, title, answer, answerType, choices = null, body = null) {
  return row({ problemName: name, rowType: "step", title, body, answer, answerType, choices });
}

function hint(name, id, title, body, dependency = null) {
  return row({ problemName: name, rowType: "hint", title, body, hintId: id, dependency });
}

function scaffold(name, id, title, body, answer, answerType, dependency, choices = null) {
  return row({ problemName: name, rowType: "scaffold", title, body, answer, answerType,
    hintId: id, dependency, choices });
}

function block(...rows) { return [...rows, Array(16).fill(null)]; }

const sources = {
  linear: "https://openstax.org/books/precalculus-2e/pages/2-3-modeling-with-linear-functions",
  rational: "https://openstax.org/books/precalculus-2e/pages/3-7-rational-functions",
  logs: "https://openstax.org/books/precalculus-2e/pages/4-6-exponential-and-logarithmic-equations",
  trig: "https://openstax.org/books/precalculus-2e/pages/7-5-solving-trigonometric-equations",
  matrices: "https://openstax.org/books/precalculus-2e/pages/9-5-matrices-and-matrix-operations",
  conics: "https://openstax.org/books/precalculus-2e/pages/10-1-the-ellipse",
  sequences: "https://openstax.org/books/precalculus-2e/pages/11-3-geometric-sequences",
  probability: "https://openstax.org/books/precalculus-2e/pages/11-7-probability",
  limits: "https://openstax.org/books/precalculus-2e/pages/12-1-finding-limits-numerically-and-graphically",
};

const suites = [
  {
    id: "sealed-a-algebra", title: "Algebra and functions",
    defects: [
      { problemName: "sealed_a_02", kind: "wrong_linear_prediction", corrections: [{ cell: "E7", expected: 23 }] },
      { problemName: "sealed_a_03", kind: "mc_answer_missing_exact_choice", corrections: [{ cell: "I11", predicate: "mc_exact_answer_once" }] },
      { problemName: "sealed_a_04", kind: "valid_mc_interaction_wrong_type", corrections: [{ cell: "F15", expected: "mc" }] },
      { problemName: "sealed_a_05", kind: "dependency_on_self", corrections: [{ cell: "H21", expected: "h1" }] },
      { problemName: "sealed_a_06", kind: "scaffold_missing_answer", corrections: [{ cell: "E26", expected: 4 }] },
      { problemName: "sealed_a_07", kind: "caret_exponent", corrections: [{ cell: "E29", expected: "x**2-6*x+8" }] },
      { problemName: "sealed_a_08", kind: "equivalent_mc_distractor", corrections: [{ cell: "I33", predicate: "mc_exact_answer_once" }] },
      { problemName: "sealed_a_11", kind: "hint_wrong_inverse_operation", corrections: [{ cell: "D46", expected: "Subtract 5 from both sides to isolate 2*x, then divide by 2." }] },
    ],
    clean: ["sealed_a_01", "sealed_a_09", "sealed_a_10", "sealed_a_12"],
    rows: [
      ...block(problem("sealed_a_01", "Slope from two points", "A line passes through (2,5) and (6,13).", sources.linear, "Linear Functions"), step("sealed_a_01", "Find the slope.", 2, "numeric"), hint("sealed_a_01", "h1", "Use the slope formula", "Compute (13-5)/(6-2).")),
      ...block(problem("sealed_a_02", "Linear population model", "A population is 500 at year 0 and increases by 30 each year.", sources.linear, "Modeling with Linear Functions"), step("sealed_a_02", "How many years until the population reaches 1190?", 17, "numeric"), hint("sealed_a_02", "h1", "Write the model", "Use P(t)=500+30*t and set P(t)=1190.")),
      ...block(problem("sealed_a_03", "Domain of a rational function", "Choose the domain of f(x)=1/(x-2).", sources.rational, "Rational Functions"), step("sealed_a_03", "Select the exact domain.", "(-inf,2)U(2,inf)", "mc", "(-inf,inf)|(-inf,2]U(2,inf)|x!=2"), hint("sealed_a_03", "h1", "Exclude a zero denominator", "The denominator is zero at x=2.")),
      ...block(problem("sealed_a_04", "Solve a linear equation", "Choose the solution of 3*x-5=7.", sources.linear, "Linear Functions"), step("sealed_a_04", "Select the solution.", "x=4", "algebra", "x=4|x=2/3|x=-4|x=12"), hint("sealed_a_04", "h1", "Undo subtraction", "Add 5, then divide by 3.")),
      ...block(problem("sealed_a_05", "Factor a quadratic", "Factor x**2-5*x+6.", sources.rational, "Polynomial Functions"), step("sealed_a_05", "Enter the factorization.", "(x-2)*(x-3)", "algebra"), hint("sealed_a_05", "h1", "Find two numbers", "Find two numbers whose product is 6 and sum is -5."), scaffold("sealed_a_05", "s1", "Check one factor", "What is one zero?", 2, "numeric", "s1")),
      ...block(problem("sealed_a_06", "Evaluate a function", "Let f(x)=2*x-7.", sources.linear, "Functions"), step("sealed_a_06", "Find f(2).", -3, "numeric"), hint("sealed_a_06", "h1", "Substitute", "Replace x with 2."), scaffold("sealed_a_06", "s1", "Multiply first", "What is 2*2?", null, "numeric", "h1")),
      ...block(problem("sealed_a_07", "Write a quadratic", "Write the quadratic with zeros 2 and 4 in expanded form.", sources.rational, "Quadratic Functions"), step("sealed_a_07", "Enter the expanded expression.", "x^2-6*x+8", "algebra"), hint("sealed_a_07", "h1", "Build factors", "Expand (x-2)*(x-4).")),
      ...block(problem("sealed_a_08", "Choose the solution pair", "Solve x+y=5 and x-y=1.", sources.linear, "Systems of Linear Equations"), step("sealed_a_08", "Select (x,y).", "(3,2)", "mc", "(3,2)|(2,3)|(6/2,2)|(4,1)"), hint("sealed_a_08", "h1", "Add the equations", "Adding eliminates y and gives 2*x=6.")),
      ...block(problem("sealed_a_09", "Vertical asymptote", "Find the vertical asymptote of f(x)=(x+1)/(x-4).", sources.rational, "Rational Functions"), step("sealed_a_09", "Enter the asymptote.", "x=4", "algebra"), hint("sealed_a_09", "h1", "Set the denominator to zero", "Solve x-4=0.")),
      ...block(problem("sealed_a_10", "Vertex of a parabola", "Find the vertex of y=(x-3)**2-5.", sources.rational, "Quadratic Functions"), step("sealed_a_10", "Enter the vertex.", "(3,-5)", "algebra"), hint("sealed_a_10", "h1", "Read vertex form", "For y=(x-h)**2+k, the vertex is (h,k).")),
      ...block(problem("sealed_a_11", "Solve an equation", "Solve 2*x+5=17.", sources.linear, "Linear Functions"), step("sealed_a_11", "Enter x.", 6, "numeric"), hint("sealed_a_11", "h1", "Isolate the variable", "Add 5 to both sides to isolate 2*x, then divide by 2.")),
      ...block(problem("sealed_a_12", "Compose functions", "Let f(x)=x+2 and g(x)=3*x.", sources.linear, "Composition of Functions"), step("sealed_a_12", "Find g(f(4)).", 18, "numeric"), hint("sealed_a_12", "h1", "Evaluate inside first", "Compute f(4), then apply g.")),
    ],
  },
  {
    id: "sealed-b-trig-logs", title: "Trigonometry and logarithms",
    defects: [
      { problemName: "sealed_b_01", kind: "wrong_exact_trig_value", corrections: [{ cell: "E3", expected: "1/2" }] },
      { problemName: "sealed_b_02", kind: "unbalanced_latex_delimiter", corrections: [{ cell: "E7", expected: "x=3" }] },
      { problemName: "sealed_b_03", kind: "valid_mc_interaction_wrong_type", corrections: [{ cell: "F11", expected: "mc" }] },
      { problemName: "sealed_b_04", kind: "inverse_trig_ratio_reversed", corrections: [{ cell: "E15", accepted: ["theta=arccos(3/5)", "theta=acos(3/5)"] }] },
      { problemName: "sealed_b_05", kind: "first_hint_has_dependency", corrections: [{ cell: "H20", predicate: "blank" }] },
      { problemName: "sealed_b_06", kind: "duplicate_hint", corrections: [{ cell: "D25", expected: "Use the unit-circle point at 3*pi/2: (0,-1)." }] },
      { problemName: "sealed_b_07", kind: "wrong_principal_sign", corrections: [{ cell: "E28", expected: "-pi/6" }] },
      { problemName: "sealed_b_08", kind: "boundary_whitespace", corrections: [{ cell: "E32", expected: "ln(8)/ln(2)" }] },
    ],
    clean: ["sealed_b_09", "sealed_b_10", "sealed_b_11", "sealed_b_12"],
    rows: [
      ...block(problem("sealed_b_01", "Exact sine value", "Evaluate sin(pi/6).", sources.trig, "Unit Circle"), step("sealed_b_01", "Enter the exact value.", "sqrt(3)/2", "numeric"), hint("sealed_b_01", "h1", "Use the unit circle", "The point at pi/6 has y-coordinate 1/2.")),
      ...block(problem("sealed_b_02", "Solve a logarithmic equation", "Solve log_2(x)=log_2(3).", sources.logs, "Logarithmic Equations"), step("sealed_b_02", "Enter the solution.", "$$x=3", "algebra"), hint("sealed_b_02", "h1", "Use one-to-one behavior", "Equal logarithms with the same base have equal arguments.")),
      ...block(problem("sealed_b_03", "Choose a cosine value", "Choose cos(pi).", sources.trig, "Unit Circle"), step("sealed_b_03", "Select the value.", -1, "numeric", "-1|0|1|1/2"), hint("sealed_b_03", "h1", "Read the x-coordinate", "At pi the unit-circle point is (-1,0).")),
      ...block(problem("sealed_b_04", "Inverse cosine in a triangle", "A right triangle has adjacent side 3 and hypotenuse 5.", sources.trig, "Inverse Trigonometric Functions"), step("sealed_b_04", "Write an exact expression for theta.", "theta=arccos(5/3)", "algebra"), hint("sealed_b_04", "h1", "Form the cosine ratio", "cos(theta)=adjacent/hypotenuse.")),
      ...block(problem("sealed_b_05", "Solve an exponential equation", "Solve 3**x=27.", sources.logs, "Exponential Equations"), step("sealed_b_05", "Enter x.", 3, "numeric"), hint("sealed_b_05", "h1", "Use a common base", "Write 27 as a power of 3.", "h9")),
      ...block(problem("sealed_b_06", "Unit-circle coordinates", "Find sin(3*pi/2).", sources.trig, "Unit Circle"), step("sealed_b_06", "Enter the value.", -1, "numeric"), hint("sealed_b_06", "h1", "Locate the angle", "Use the unit-circle point at 3*pi/2."), hint("sealed_b_06", "h2", "Read the sine coordinate", "Use the unit-circle point at 3*pi/2.", "h1")),
      ...block(problem("sealed_b_07", "Principal arctangent", "Evaluate arctan(-1/sqrt(3)).", sources.trig, "Inverse Trigonometric Functions"), step("sealed_b_07", "Enter the principal value.", "pi/6", "numeric"), hint("sealed_b_07", "h1", "Use the principal interval", "The arctangent principal interval is (-pi/2,pi/2).")),
      ...block(problem("sealed_b_08", "Change of base", "Use change of base to evaluate log_2(8).", sources.logs, "Logarithmic Functions"), step("sealed_b_08", "Enter the change-of-base expression.", " ln(8)/ln(2) ", "algebra"), hint("sealed_b_08", "h1", "Apply the formula", "Use log_b(a)=ln(a)/ln(b).")),
      ...block(problem("sealed_b_09", "Double-angle identity", "Evaluate sin(2*theta) when sin(theta)=3/5 and cos(theta)=4/5.", sources.trig, "Trigonometric Identities"), step("sealed_b_09", "Enter the exact value.", "24/25", "numeric"), hint("sealed_b_09", "h1", "Use the identity", "sin(2*theta)=2*sin(theta)*cos(theta).")),
      ...block(problem("sealed_b_10", "Logarithm property", "Expand ln(x**3) for x>0.", sources.logs, "Logarithmic Properties"), step("sealed_b_10", "Enter the expanded form.", "3*ln(x)", "algebra"), hint("sealed_b_10", "h1", "Use the power rule", "Bring the exponent in front of the logarithm.")),
      ...block(problem("sealed_b_11", "Solve a sine equation", "Solve sin(x)=0 on [0,2*pi].", sources.trig, "Trigonometric Equations"), step("sealed_b_11", "Enter the solution set.", "{0,pi,2*pi}", "algebra"), hint("sealed_b_11", "h1", "Use unit-circle zeros", "Sine is zero on the horizontal axis.")),
      ...block(problem("sealed_b_12", "Exponential growth", "A quantity starts at 80 and doubles every 5 years.", sources.logs, "Exponential Models"), step("sealed_b_12", "Find its value after 15 years.", 640, "numeric"), hint("sealed_b_12", "h1", "Count doubling periods", "Fifteen years contains three five-year periods.")),
    ],
  },
  {
    id: "sealed-c-matrices-conics", title: "Matrices, systems, and conics",
    defects: [
      { problemName: "sealed_c_01", kind: "wrong_matrix_dimension", corrections: [{ cell: "E3", expected: "2x3" }] },
      { problemName: "sealed_c_02", kind: "equivalent_mc_distractor", corrections: [{ cell: "I7", predicate: "mc_exact_answer_once" }] },
      { problemName: "sealed_c_03", kind: "incomplete_system_solution", corrections: [{ cell: "E11", expected: "(2,1)" }] },
      { problemName: "sealed_c_04", kind: "row_shift_right", corrections: [{ cell: "C15", expected: "Find the determinant." }, { cell: "D15", predicate: "blank" }, { cell: "E15", expected: -2 }, { cell: "F15", expected: "numeric" }, { cell: "G15", predicate: "blank" }] },
      { problemName: "sealed_c_05", kind: "dependency_crosses_step", corrections: [{ cell: "H22", predicate: "blank" }] },
      { problemName: "sealed_c_06", kind: "scaffold_wrong_answer", corrections: [{ cell: "E27", expected: 9 }] },
      { problemName: "sealed_c_07", kind: "invalid_answer_type", corrections: [{ cell: "F30", expected: "algebra" }] },
      { problemName: "sealed_c_08", kind: "metadata_on_non_problem_row", corrections: [{ cell: "M36", predicate: "blank" }] },
    ],
    clean: ["sealed_c_09", "sealed_c_10", "sealed_c_11", "sealed_c_12"],
    rows: [
      ...block(problem("sealed_c_01", "Matrix dimensions", "A matrix has 2 rows and 3 columns.", sources.matrices, "Matrices and Matrix Operations"), step("sealed_c_01", "Enter its dimensions.", "3x2", "algebra"), hint("sealed_c_01", "h1", "Rows first", "Matrix dimensions are written rows by columns.")),
      ...block(problem("sealed_c_02", "Add two matrices", "Let A=[[1,2],[3,4]] and B=[[2,0],[1,-1]].", sources.matrices, "Matrices and Matrix Operations"), step("sealed_c_02", "Choose A+B.", "[[3,2],[4,3]]", "mc", "[[3,2],[4,3]]|[[3,2],[4,3]]|[[2,2],[3,-4]]|[[1,0],[3,-1]]"), hint("sealed_c_02", "h1", "Add corresponding entries", "Add entries in the same row and column positions.")),
      ...block(problem("sealed_c_03", "Solve a linear system", "Solve x+y=3 and x-y=1.", sources.matrices, "Systems of Linear Equations"), step("sealed_c_03", "Enter the ordered pair.", "x=2", "algebra"), hint("sealed_c_03", "h1", "Add equations", "Adding gives 2*x=4; then substitute to find y.")),
      ...block(problem("sealed_c_04", "Determinant of a matrix", "Find det([[1,2],[3,4]]).", sources.matrices, "Matrices and Matrix Operations"), row({ problemName: "sealed_c_04", rowType: "step", body: "Find the determinant.", answerType: -2, hintId: "numeric" }), hint("sealed_c_04", "h1", "Use ad-bc", "Compute 1*4-2*3.")),
      ...block(problem("sealed_c_05", "Two-step elimination", "Solve 2*x+y=7 and x-y=2.", sources.matrices, "Systems of Linear Equations"), step("sealed_c_05", "First find x.", 3, "numeric"), hint("sealed_c_05", "h1", "Add equations", "Adding the equations gives 3*x=9."), step("sealed_c_05", "Then find y.", 1, "numeric"), hint("sealed_c_05", "h2", "Substitute", "Use x=3 in x-y=2.", "h1")),
      ...block(problem("sealed_c_06", "Ellipse semi-axis", "For x**2/25+y**2/9=1, find b**2.", sources.conics, "The Ellipse"), step("sealed_c_06", "Enter b**2.", 9, "numeric"), hint("sealed_c_06", "h1", "Read standard form", "The denominators are the squared semi-axis lengths."), scaffold("sealed_c_06", "s1", "Identify the smaller denominator", "What is the smaller denominator?", 25, "numeric", "h1")),
      ...block(problem("sealed_c_07", "Parabola equation", "Write the parabola with vertex (0,0) and focus (0,2).", sources.conics, "The Parabola"), step("sealed_c_07", "Enter the equation.", "x**2=8*y", "string"), hint("sealed_c_07", "h1", "Use standard form", "For a vertical parabola, x**2=4*p*y.")),
      ...block(problem("sealed_c_08", "Matrix scalar multiplication", "Multiply [[1,-2],[0,3]] by 3.", sources.matrices, "Matrices and Matrix Operations"), step("sealed_c_08", "Enter the result.", "[[3,-6],[0,9]]", "algebra"), hint("sealed_c_08", "h1", "Multiply every entry", "Apply the scalar to each matrix entry."), row({ problemName: "sealed_c_08", rowType: "scaffold", title: "Check one entry", body: "What is 3*(-2)?", answer: -6, answerType: "numeric", hintId: "s1", dependency: "h1", openstaxKc: "Matrices and Matrix Operations" })),
      ...block(problem("sealed_c_09", "Ellipse center", "Find the center of (x-2)**2/16+(y+1)**2/9=1.", sources.conics, "The Ellipse"), step("sealed_c_09", "Enter the center.", "(2,-1)", "algebra"), hint("sealed_c_09", "h1", "Read translated form", "The center is (h,k) in (x-h)**2/a**2+(y-k)**2/b**2=1.")),
      ...block(problem("sealed_c_10", "Matrix trace", "Find the trace of [[4,1],[2,-3]].", sources.matrices, "Matrices and Matrix Operations"), step("sealed_c_10", "Enter the trace.", 1, "numeric"), hint("sealed_c_10", "h1", "Add diagonal entries", "Compute 4+(-3).")),
      ...block(problem("sealed_c_11", "Circle radius", "Find the radius of (x-1)**2+(y+2)**2=49.", sources.conics, "Conic Sections"), step("sealed_c_11", "Enter the radius.", 7, "numeric"), hint("sealed_c_11", "h1", "Compare with standard form", "The right side is r**2.")),
      ...block(problem("sealed_c_12", "Matrix product entry", "Let A=[[1,2],[0,1]] and B=[[3,1],[2,4]].", sources.matrices, "Matrices and Matrix Operations"), step("sealed_c_12", "Find the (1,1) entry of A*B.", 7, "numeric"), hint("sealed_c_12", "h1", "Use row times column", "Compute 1*3+2*2.")),
    ],
  },
  {
    id: "sealed-d-sequences-probability", title: "Sequences, probability, and limits",
    defects: [
      { problemName: "sealed_d_01", kind: "excel_date_coercion", corrections: [{ cell: "E3", expected: "1/2" }] },
      { problemName: "sealed_d_02", kind: "wrong_geometric_term", corrections: [{ cell: "E7", expected: 162 }] },
      { problemName: "sealed_d_03", kind: "mc_answer_missing_exact_choice", corrections: [{ cell: "I11", predicate: "mc_exact_answer_once" }] },
      { problemName: "sealed_d_04", kind: "step_missing_answer", corrections: [{ cell: "E15", expected: 10 }] },
      { problemName: "sealed_d_05", kind: "dependency_on_self", corrections: [{ cell: "H21", expected: "h1" }] },
      { problemName: "sealed_d_06", kind: "hint_wrong_range", corrections: [{ cell: "D25", expected: "For 2**x, every output is positive and 0 is approached but never reached." }] },
      { problemName: "sealed_d_07", kind: "caret_exponent", corrections: [{ cell: "E28", expected: "a_n=4*3**(n-1)" }] },
      { problemName: "sealed_d_08", kind: "valid_mc_interaction_wrong_type", corrections: [{ cell: "F32", expected: "mc" }] },
    ],
    clean: ["sealed_d_09", "sealed_d_10", "sealed_d_11", "sealed_d_12"],
    rows: [
      ...block(problem("sealed_d_01", "Probability of an even roll", "A fair six-sided die is rolled once.", sources.probability, "Probability"), step("sealed_d_01", "Find P(even).", new Date("2026-01-02T00:00:00Z"), "numeric"), hint("sealed_d_01", "h1", "Count outcomes", "Three of the six outcomes are even.")),
      ...block(problem("sealed_d_02", "Geometric sequence term", "A geometric sequence has a_1=2 and ratio r=3.", sources.sequences, "Geometric Sequences"), step("sealed_d_02", "Find a_5.", 54, "numeric"), hint("sealed_d_02", "h1", "Use the nth-term formula", "a_n=a_1*r**(n-1).")),
      ...block(problem("sealed_d_03", "Probability without replacement", "A bag has 3 red and 2 blue marbles. Two are drawn without replacement.", sources.probability, "Probability"), step("sealed_d_03", "Select P(red then blue).", "3/10", "mc", "3/5|2/5|6/25|1/5"), hint("sealed_d_03", "h1", "Multiply conditional probabilities", "Use (3/5)*(2/4).")),
      ...block(problem("sealed_d_04", "Arithmetic sequence term", "An arithmetic sequence has a_1=4 and common difference 2.", sources.sequences, "Arithmetic Sequences"), step("sealed_d_04", "Find a_4.", null, "numeric"), hint("sealed_d_04", "h1", "Add the common difference", "Use a_n=a_1+(n-1)*d.")),
      ...block(problem("sealed_d_05", "Finite geometric sum", "Find 1+1/2+1/4+1/8.", sources.sequences, "Series"), step("sealed_d_05", "Enter the sum.", "15/8", "numeric"), hint("sealed_d_05", "h1", "Use a common denominator", "Write every term over 8."), scaffold("sealed_d_05", "s1", "Add numerators", "What is 8+4+2+1?", 15, "numeric", "s1")),
      ...block(problem("sealed_d_06", "Range of an exponential function", "Describe the range of f(x)=2**x.", sources.limits, "Exponential Functions"), step("sealed_d_06", "Enter the interval.", "(0,inf)", "algebra"), hint("sealed_d_06", "h1", "Track outputs", "For 2**x, outputs are nonnegative and 0 is included.")),
      ...block(problem("sealed_d_07", "Geometric sequence formula", "A geometric sequence begins 4,12,36,...", sources.sequences, "Geometric Sequences"), step("sealed_d_07", "Write a_n.", "a_n=4*3^(n-1)", "algebra"), hint("sealed_d_07", "h1", "Identify the ratio", "Each term is multiplied by 3.")),
      ...block(problem("sealed_d_08", "Choose a binomial probability", "A fair coin is tossed twice.", sources.probability, "Probability"), step("sealed_d_08", "Select P(exactly one head).", "1/2", "algebra", "1/2|1/4|3/4|1"), hint("sealed_d_08", "h1", "List the outcomes", "The outcomes HT and TH are favorable.")),
      ...block(problem("sealed_d_09", "Limit of a polynomial", "Find lim_(x->2) (x**2+1).", sources.limits, "Finding Limits"), step("sealed_d_09", "Enter the limit.", 5, "numeric"), hint("sealed_d_09", "h1", "Use continuity", "Substitute x=2 into the polynomial.")),
      ...block(problem("sealed_d_10", "Combination count", "How many ways can 2 students be chosen from 5?", sources.probability, "Counting Principles"), step("sealed_d_10", "Enter the count.", 10, "numeric"), hint("sealed_d_10", "h1", "Use a combination", "Compute C(5,2).")),
      ...block(problem("sealed_d_11", "Infinite geometric sum", "Find the sum of 6+3+3/2+...", sources.sequences, "Series"), step("sealed_d_11", "Enter the sum.", 12, "numeric"), hint("sealed_d_11", "h1", "Use the infinite-sum formula", "Use S=a_1/(1-r) with r=1/2.")),
      ...block(problem("sealed_d_12", "One-sided limit", "Find lim_(x->0+) sqrt(x).", sources.limits, "Finding Limits"), step("sealed_d_12", "Enter the limit.", 0, "numeric"), hint("sealed_d_12", "h1", "Approach from the domain", "Positive x values produce square roots approaching 0.")),
    ],
  },
];

await fs.mkdir(workbookDir, { recursive: true });
await fs.mkdir(keyDir, { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const summaries = [];
for (const suite of suites) {
  const workbook = Workbook.create();
  const sheet = workbook.worksheets.add("Problems");
  const values = [HEADERS, ...suite.rows];
  sheet.getRangeByIndexes(0, 0, values.length, HEADERS.length).values = values;
  const used = sheet.getRangeByIndexes(0, 0, values.length, HEADERS.length);
  used.format.font = { name: "Arial", size: 10, color: "#000000" };
  used.format.verticalAlignment = "top";
  used.format.wrapText = true;
  sheet.getRange("A1:P1").format.font = { name: "Arial", size: 10, bold: true, color: "#000000" };
  sheet.getRange("A:A").format.columnWidth = 22;
  sheet.getRange("B:B").format.columnWidth = 12;
  sheet.getRange("C:C").format.columnWidth = 31;
  sheet.getRange("D:D").format.columnWidth = 54;
  sheet.getRange("E:E").format.columnWidth = 24;
  sheet.getRange("F:H").format.columnWidth = 15;
  sheet.getRange("I:I").format.columnWidth = 52;
  sheet.getRange("J:K").format.columnWidth = 18;
  sheet.getRange("L:L").format.columnWidth = 40;
  sheet.getRange("M:P").format.columnWidth = 24;
  sheet.freezePanes.freezeRows(1);
  sheet.showGridLines = true;

  const inspection = await workbook.inspect({ kind: "table", sheetId: "Problems",
    range: `A1:P${values.length}`, include: "values,formulas", tableMaxRows: 8,
    tableMaxCols: 16, maxChars: 9000 });
  const errorScan = await workbook.inspect({ kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
    options: { useRegex: true, maxResults: 100 }, summary: "final formula error scan" });
  const preview = await workbook.render({ sheetName: "Problems",
    range: `A1:I${Math.min(values.length, 28)}`, scale: 1, format: "png" });
  const previewBytes = new Uint8Array(await preview.arrayBuffer());
  await fs.writeFile(`${previewDir}/${suite.id}.png`, previewBytes);

  const output = await SpreadsheetFile.exportXlsx(workbook);
  const workbookPath = `${workbookDir}/${suite.id}.xlsx`;
  await output.save(workbookPath);

  const key = { schemaVersion: 2, suite: suite.id, title: suite.title,
    workbook: `${suite.id}.xlsx`, expectedProblemCount: 12,
    expectedDefectGroupCount: suite.defects.length, expectedCleanControlCount: suite.clean.length,
    instructions: "Keep sealed until the council returns a corrected workbook. Do not send this key to the council.",
    defectGroups: suite.defects,
    cleanControls: suite.clean.map((problemName) => ({ problemName })) };
  await fs.writeFile(`${keyDir}/${suite.id}.answer-key.json`, `${JSON.stringify(key, null, 2)}\n`);
  summaries.push({ id: suite.id, workbookPath, rows: values.length,
    defects: suite.defects.length, clean: suite.clean.length,
    inspectionBytes: inspection.ndjson.length, errorScan: errorScan.ndjson,
    previewBytes: previewBytes.byteLength });
}

console.log(JSON.stringify(summaries, null, 2));
