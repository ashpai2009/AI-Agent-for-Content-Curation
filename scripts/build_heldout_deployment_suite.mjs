import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const HEADERS = [
  "Problem Name",
  "Row Type",
  "Title",
  "Body Text",
  "Answer",
  "answerType",
  "HintID",
  "Dependency",
  "mcChoices",
  "Images (space delimited)",
  "Parent",
  "OER src",
  "openstax KC",
  "KC",
  "Taxonomy",
  "License",
];

const OER = "https://openstax.org/books/precalculus-2e/pages/3-introduction-to-exponential-and-logarithmic-functions";

function blankRow() {
  return Array(HEADERS.length).fill(null);
}

class HeldOutBook {
  constructor(filename, { notation, dependencyConvention, topic }) {
    this.filename = filename;
    this.notation = notation;
    this.dependencyConvention = dependencyConvention;
    this.topic = topic;
    this.rows = [HEADERS];
    this.defectGroups = [];
    this.cleanControls = [];
  }

  add(values) {
    const row = blankRow();
    for (const [key, value] of Object.entries(values)) {
      row[HEADERS.indexOf(key)] = value;
    }
    this.rows.push(row);
    return this.rows.length;
  }

  addRaw(row) {
    if (row.length !== HEADERS.length) throw new Error("raw row has wrong width");
    this.rows.push(row);
    return this.rows.length;
  }

  problem(name, title, body = "") {
    return this.add({
      "Problem Name": name,
      "Row Type": "problem",
      Title: title,
      "Body Text": body,
      "OER src": OER,
      "openstax KC": this.topic,
      KC: this.topic.toLowerCase().replaceAll(" ", "-"),
      Taxonomy: "OpenStax",
      License: "CC BY 4.0",
    });
  }

  step(name, title, answer, answerType, mcChoices = null) {
    return this.add({
      "Problem Name": name,
      "Row Type": "step",
      Title: title,
      Answer: answer,
      answerType,
      mcChoices,
    });
  }

  hint(name, id, title, body, dependency = null) {
    return this.add({
      "Problem Name": name,
      "Row Type": "hint",
      Title: title,
      "Body Text": body,
      HintID: id,
      Dependency: dependency,
    });
  }

  scaffold(name, id, dependency, title, body, answer, answerType) {
    return this.add({
      "Problem Name": name,
      "Row Type": "scaffold",
      Title: title,
      "Body Text": body,
      Answer: answer,
      answerType,
      HintID: id,
      Dependency: dependency,
    });
  }

  defect(problemName, kind, corrections, note) {
    this.defectGroups.push({ problemName, kind, corrections, note });
  }

  clean(problemName, note) {
    this.cleanControls.push({ problemName, note });
  }

  key() {
    return {
      schemaVersion: 2,
      workbook: this.filename,
      difficulty: "held-out-deployment-adversarial",
      topic: this.topic,
      expectedProblemCount: 15,
      expectedNotation: this.notation,
      expectedDependencyConvention: this.dependencyConvention,
      defectGroups: this.defectGroups,
      cleanControls: this.cleanControls,
    };
  }
}

const exact = (cell, expected) => ({ cell, expected });
const accepted = (cell, values) => ({ cell, accepted: values });
const predicate = (cell, name) => ({ cell, predicate: name });

function algebraBook() {
  const b = new HeldOutBook("heldout-01-algebra-functions.xlsx", {
    notation: "ascii",
    dependencyConvention: "reset_per_step",
    topic: "Algebra and Functions",
  });

  b.problem("algx1", "Solve a multistep linear equation", "Solve 3(2x-5)=21.");
  let r = b.step("algx1", "Find x.", "5", "numeric");
  b.hint("algx1", "h1", "Distribute", "Write 6x-15=21.");
  b.scaffold("algx1", "s1", "h1", "Undo subtraction", "Add 15 to both sides.", "36", "numeric");
  b.defect("algx1", "wrong_linear_solution", [exact(`E${r}`, "6")], "The final division is by 6.");

  b.problem("algx2", "Simplify a rational expression", "Simplify (x**2-9)/(x-3), with x!=3.");
  b.step("algx2", "State the simplified expression.", "x+3", "algebra");
  b.hint("algx2", "h1", "Factor the numerator", "Use x**2-9=(x-3)*(x+3).");
  b.clean("algx2", "The cancellation and domain restriction are already correct.");

  b.problem("algx3", "Check an extraneous radical solution", "Solve sqrt(x+5)=x-1 over the reals.");
  r = b.step("algx3", "List every solution.", "x=-1,x=4", "algebra");
  b.hint("algx3", "h1", "Respect the domain", "The right side x-1 must be nonnegative.");
  b.scaffold("algx3", "s1", "h1", "Test candidates", "Substitute each candidate into the original equation.", "4", "numeric");
  b.defect("algx3", "extraneous_radical_root", [accepted(`E${r}`, ["4", "x=4"])], "x=-1 fails the original equation.");

  b.problem("algx4", "Solve an exponential equation", "Solve 2**(x+1)=16.");
  b.step("algx4", "State the solution as an equation.", "x=3", "algebra");
  b.hint("algx4", "h1", "Use a common base", "Write 16 as 2**4.");
  b.clean("algx4", "The equation-form answer is correct and should not be normalized to a scalar.");

  b.problem("algx5", "Factor a quadratic", "Factor x**2-5*x+6.");
  r = b.step("algx5", "Give the factored form.", "(x-2)*(x-3)", "numeric");
  b.hint("algx5", "h1", "Find two integers", "Their product is 6 and their sum is -5.");
  b.defect("algx5", "answer_type_mismatch", [exact(`F${r}`, "algebra")], "A variable expression is algebraic.");

  b.problem("algx6", "Choose an exact probability", "A fair coin is tossed twice. Choose the probability of exactly one head.");
  r = b.step("algx6", "Select the exact value.", "1/2", "mc", "0.5|1/3|2/4|3/4");
  b.hint("algx6", "h1", "List outcomes", "The equally likely outcomes are HH, HT, TH, and TT.");
  b.defect("algx6", "mc_exact_answer_and_equivalent_distractors", [predicate(`I${r}`, "mc_exact_answer_once")], "The exact Answer must occur once and equivalent distractors must be removed.");

  b.problem("algx7", "Solve two related steps", "For f(x)=x**2-4*x+1, find the axis and vertex value.");
  b.step("algx7", "Find the axis of symmetry.", "x=2", "algebra");
  b.hint("algx7", "h1", "Use -b/(2a)", "Compute -(-4)/(2*1).");
  b.scaffold("algx7", "s3", "h1", "Evaluate the function", "Substitute x=2 into f.", "-3", "numeric");
  b.step("algx7", "State the minimum value.", "-3", "numeric");
  b.hint("algx7", "h1", "Use the vertex", "The leading coefficient is positive.");
  b.clean("algx7", "The unused scaffold label s2 is a valid gap, and reset-per-step h1 labels are correct.");

  b.problem("algx8", "Undo subtraction", "Solve y-8=13.");
  b.step("algx8", "Find y.", "21", "numeric");
  r = b.hint("algx8", "h1", "Add 8", "Subtract 8 from both sides to obtain y=21.");
  b.defect("algx8", "hint_operation_contradiction", [exact(`D${r}`, "Add 8 to both sides to obtain y=21.")], "The hint body must perform the operation named by its title.");

  b.problem("algx9", "Solve while preserving a valid exact form", "Solve x**2=49 for the nonnegative root.");
  b.step("algx9", "State x as an exact equation.", "x=sqrt(49)", "algebra");
  b.hint("algx9", "h1", "Use the principal root", "The requested root is nonnegative.");
  b.clean("algx9", "x=sqrt(49) is a correct exact algebraic answer; replacing it with 7 is unnecessary.");

  b.problem("algx10", "Use substitution in a system", "Given x+y=5 and y=2, find x.");
  b.step("algx10", "State x.", "3", "numeric");
  b.hint("algx10", "h1", "Substitute y", "Write x+2=5.");
  r = b.scaffold("algx10", "s1", "h1", "Isolate x", "Subtract 2 from both sides.", null, "numeric");
  b.defect("algx10", "scaffold_missing_answer", [exact(`E${r}`, "3")], "A scaffold is graded and must carry its answer.");

  b.problem("algx11", "State an ordered pair", "A line has x-intercept 2 and y-intercept 5.");
  r = b.step("algx11", "Give the pair of intercept values.", "(2,5) ", "algebra");
  b.hint("algx11", "h1", "Use the requested order", "Enter x first and y second.");
  b.defect("algx11", "trailing_whitespace", [exact(`E${r}`, "(2,5)")], "Graded values cannot carry invisible trailing whitespace.");

  b.problem("algx12", "Solve a logarithmic equation", "Solve log_2(x-1)=3.");
  b.step("algx12", "Find x.", "9", "numeric");
  b.hint("algx12", "h1", "Rewrite exponentially", "Use x-1=2**3.");
  b.clean("algx12", "The value and domain are correct.");

  b.problem("algx13", "Solve an absolute-value equation", "Solve abs(2x-1)=7.");
  b.step("algx13", "List both solutions.", "x=-3,x=4", "algebra");
  r = b.hint("algx13", "h1", "Split into two equations", "Use 2x-1=7 and 2x-1=-7.");
  const metadataCells = ["L", "M", "N", "O", "P"];
  const metadataValues = [OER, "Algebra and Functions", "algebra-and-functions", "OpenStax", "CC BY 4.0"];
  metadataCells.forEach((column, index) => { b.rows[r - 1][11 + index] = metadataValues[index]; });
  b.defect("algx13", "metadata_on_hint", metadataCells.map((column) => exact(`${column}${r}`, null)), "Metadata belongs only on the problem row.");

  b.problem("algx14", "Choose the solution", "Solve 4x+8=0.");
  b.step("algx14", "Select x.", "-2", "mc", "0|2|-2|4");
  b.hint("algx14", "h1", "Subtract 8", "Then divide by 4.");
  b.clean("algx14", "The exact Answer is present once and need not be the first choice.");

  b.problem("algx15", "Find a quadratic minimum", "For f(x)=x**2-6*x+5, find the minimum value.");
  b.step("algx15", "Find the vertex x-coordinate.", "3", "numeric");
  b.hint("algx15", "h1", "Use -b/(2a)", "Compute 6/2.");
  r = b.step("algx15", "Evaluate f at the vertex.", "-3", "numeric");
  b.hint("algx15", "h1", "Substitute carefully", "Compute 3**2-6*3+5.");
  b.defect("algx15", "wrong_value_propagation", [exact(`E${r}`, "-4")], "The vertex value is 9-18+5=-4.");
  return b;
}

function trigBook() {
  const b = new HeldOutBook("heldout-02-trigonometry.xlsx", {
    notation: "latex",
    dependencyConvention: "continuous",
    topic: "Trigonometry",
  });

  b.problem("trigh1", "Solve on one period", "Solve $$\\sin(\\theta)=\\frac{1}{2}$$ for $$0\\leq\\theta<2\\pi$$.");
  let r = b.step("trigh1", "List every solution.", "$$\\theta=\\frac{\\pi}{6}$$", "algebra");
  b.hint("trigh1", "h1", "Find the reference angle", "The reference angle is $$\\frac{\\pi}{6}$$.");
  b.hint("trigh1", "h2", "Use sine's signs", "Sine is positive in quadrants I and II.", "h1");
  b.defect("trigh1", "incomplete_solution_set", [exact(`E${r}`, "$$\\theta=\\frac{\\pi}{6},\\frac{5\\pi}{6}$$")], "Both quadrant-I and quadrant-II solutions are required.");

  b.problem("trigh2", "Verify a double-angle identity", "Simplify $$1-2\\sin^2(x)$$.");
  b.step("trigh2", "Give an equivalent expression.", "$$\\cos(2x)$$", "algebra");
  b.hint("trigh2", "h1", "Recall a double-angle form", "Use $$\\cos(2x)=1-2\\sin^2(x)$$.");
  b.clean("trigh2", "The non-expanded identity is already correct.");

  b.problem("trigh3", "Use a special-angle value", "Evaluate cosine at a quadrant-I angle.");
  r = b.step("trigh3", "Find cos(θ) when θ=pi/3.", "$$\\frac{1}{2}$$", "algebra");
  b.hint("trigh3", "h1", "Use the unit circle", "The point at $$\\frac{\\pi}{3}$$ has x-coordinate $$\\frac{1}{2}$$.");
  b.defect("trigh3", "raw_unicode_math", [accepted(`C${r}`, ["Find cos(theta) when theta=pi/3.", "Find $$\\cos(\\theta)$$ when $$\\theta=\\frac{\\pi}{3}$$."])], "Unicode theta must be replaced with ASCII prose or contained LaTeX.");

  b.problem("trigh4", "Choose an exact unit-circle value", "Choose $$\\cos(\\frac{\\pi}{4})$$.");
  r = b.step("trigh4", "Select the exact value.", "$$\\frac{\\sqrt{2}}{2}$$", "mc", "$$0.7071$$|$$\\frac{1}{\\sqrt{2}}$$|$$\\frac{1}{2}$$|$$\\frac{\\sqrt{3}}{2}$$");
  b.hint("trigh4", "h1", "Use a 45-45-90 triangle", "The legs have equal length.");
  b.defect("trigh4", "mc_equivalent_without_exact_answer", [predicate(`I${r}`, "mc_exact_answer_once")], "The exact answer must appear once and equivalent forms cannot remain as distractors.");

  b.problem("trigh5", "Use continuous hint labels", "Solve $$2\\sin(x)=\\sqrt{3}$$, then identify the quadrant.");
  b.step("trigh5", "Find the reference angle.", "$$\\frac{\\pi}{3}$$", "algebra");
  b.hint("trigh5", "h1", "Isolate sine", "Write $$\\sin(x)=\\frac{\\sqrt{3}}{2}$$.");
  b.hint("trigh5", "h2", "Read the unit circle", "The reference angle is $$\\frac{\\pi}{3}$$.", "h1");
  b.step("trigh5", "Name a quadrant where sine is positive.", "I", "algebra");
  b.hint("trigh5", "h3", "Use sine's sign", "Sine is positive above the x-axis.");
  b.clean("trigh5", "Continuous labels continue to h3, while the first hint under the new step has no dependency.");

  b.problem("trigh6", "Apply a second trigonometric step", "Find a reference angle and then a coterminal angle.");
  b.step("trigh6", "Find the reference angle for 7pi/6.", "$$\\frac{\\pi}{6}$$", "algebra");
  b.hint("trigh6", "h1", "Subtract pi", "Use $$\\frac{7\\pi}{6}-\\pi$$.");
  b.hint("trigh6", "h2", "Simplify", "The difference is $$\\frac{\\pi}{6}$$.", "h1");
  b.step("trigh6", "Find a positive coterminal angle for -pi/3.", "$$\\frac{5\\pi}{3}$$", "algebra");
  r = b.hint("trigh6", "h3", "Add one revolution", "Add $$2\\pi$$.", "h2");
  b.defect("trigh6", "first_hint_crosses_step", [exact(`H${r}`, null)], "The first hint under a new step has no dependency even with continuous labels.");

  b.problem("trigh7", "Choose the larger solution", "Solve $$2\\cos(x)=1$$ on $$0\\leq x<2\\pi$$ and report the larger solution.");
  r = b.step("trigh7", "State the larger solution.", "$$x=\\frac{\\pi}{3}$$", "algebra");
  b.hint("trigh7", "h1", "Find both solutions", "Cosine is positive in quadrants I and IV.");
  b.defect("trigh7", "requested_order_solution", [exact(`E${r}`, "$$x=\\frac{5\\pi}{3}$$")], "The question requests the larger of the two solutions.");

  b.problem("trigh8", "Rewrite a tangent identity", "Simplify $$\\frac{2\\tan(x)}{1-\\tan^2(x)}$$.");
  b.step("trigh8", "Give the equivalent function.", "$$\\tan(2x)$$", "algebra");
  b.hint("trigh8", "h1", "Use the tangent double angle", "Match numerator and denominator directly.");
  b.clean("trigh8", "The identity is correct in exact form.");

  b.problem("trigh9", "Convert degrees to radians", "Convert $$150^{\\circ}$$ to radians.");
  r = b.step("trigh9", "State the radian measure.", "$$\\frac{5\\pi}{6}^{\\circ}$$", "algebra");
  b.hint("trigh9", "h1", "Multiply by pi/180", "Use $$150\\cdot\\frac{\\pi}{180}$$.");
  b.defect("trigh9", "degree_unit_retained_in_radian_answer", [exact(`E${r}`, "$$\\frac{5\\pi}{6}$$")], "A radian measure does not carry a degree symbol.");

  b.problem("trigh10", "Undo a trigonometric coefficient", "Solve $$3\\sin(x)-2=1$$ for $$\\sin(x)$$.");
  b.step("trigh10", "Isolate sine.", "$$\\sin(x)=1$$", "algebra");
  r = b.hint("trigh10", "h1", "Add 2", "Subtract 2 from both sides, then divide by 3.");
  b.defect("trigh10", "hint_operation_contradiction", [exact(`D${r}`, "Add 2 to both sides, then divide by 3.")], "The stated operation must produce 3sin(x)=3.");

  b.problem("trigh11", "State a trigonometric equation", "Give an equation whose solutions are integer multiples of pi.");
  b.step("trigh11", "State the equation.", "$$\\sin(\\theta)=0$$", "algebra");
  b.hint("trigh11", "h1", "Use x-axis intersections", "Sine is zero on the x-axis.");
  b.clean("trigh11", "The full equation is a valid algebraic answer and must not be reduced to 0.");

  b.problem("trigh12", "Use a half-angle identity", "Find $$\\sin(\\frac{x}{2})$$ from a known cosine value.");
  r = b.add({
    "Problem Name": "trigh12",
    "Row Type": "step",
    Title: "Choose the correct sign from the quadrant.",
    "Body Text": "The angle x/2 lies in quadrant II, so sine is positive.",
    HintID: "h1",
  });
  b.defect("trigh12", "hint_row_mislabeled_as_step", [exact(`B${r}`, "hint")], "The row has hint content and an identifier, not a graded answer.");

  b.problem("trigh13", "Use a scaffold in a continuous chain", "Solve $$\\tan(x)=1$$ on the first quadrant.");
  b.step("trigh13", "Find x.", "$$\\frac{\\pi}{4}$$", "algebra");
  b.hint("trigh13", "h1", "Use a special angle", "Tangent is one when sine and cosine agree.");
  b.scaffold("trigh13", "s1", "h1", "Check the ratio", "Evaluate sine divided by cosine at pi/4.", "1", "numeric");
  b.clean("trigh13", "The scaffold and dependency are correct.");

  b.problem("trigh14", "Evaluate an exact secant", "Find $$\\sec(\\frac{\\pi}{3})$$.");
  r = b.step("trigh14", "State the exact value.", "$$2", "numeric");
  b.hint("trigh14", "h1", "Use the reciprocal", "Cosine at pi/3 is one half.");
  b.defect("trigh14", "unbalanced_latex_delimiter", [exact(`E${r}`, "2")], "A plain graded value needs no delimiters, and the source delimiter is unbalanced.");

  b.problem("trigh15", "Solve a quadratic in sine", "Solve $$2\\sin^2(x)-3\\sin(x)+1=0$$ on $$0\\leq x<2\\pi$$.");
  r = b.step("trigh15", "List every solution.", "$$x=\\frac{\\pi}{6},\\frac{5\\pi}{6}$$", "algebra");
  b.hint("trigh15", "h1", "Factor in sine", "Use $$(2\\sin(x)-1)(\\sin(x)-1)=0$$.");
  b.hint("trigh15", "h2", "Solve both branches", "Use sine equal to one half and sine equal to one.", "h1");
  b.defect("trigh15", "omitted_factor_branch", [exact(`E${r}`, "$$x=\\frac{\\pi}{6},\\frac{\\pi}{2},\\frac{5\\pi}{6}$$")], "The sin(x)=1 branch contributes pi/2.");
  return b;
}

function modelingBook() {
  const b = new HeldOutBook("heldout-03-functions-modeling.xlsx", {
    notation: "ascii",
    dependencyConvention: "continuous",
    topic: "Functions and Modeling",
  });

  b.problem("modelx1", "Evaluate a composition", "Let f(x)=2*x+3 and g(x)=x**2. Find f(g(2)).");
  let r = b.step("modelx1", "Evaluate the composition.", "25", "numeric");
  b.hint("modelx1", "h1", "Evaluate the inner function", "First compute g(2)=4.");
  b.scaffold("modelx1", "s1", "h1", "Apply f", "Substitute 4 into 2*x+3.", "11", "numeric");
  b.defect("modelx1", "composition_order_error", [exact(`E${r}`, "11")], "f(g(2))=f(4)=11.");

  b.problem("modelx2", "Find a difference quotient", "For f(x)=x**2+3*x, simplify (f(x+h)-f(x))/h.");
  b.step("modelx2", "Give the simplified quotient.", "2*x+h+3", "algebra");
  b.hint("modelx2", "h1", "Expand f(x+h)", "Use (x+h)**2+3*(x+h).");
  b.clean("modelx2", "The quotient is correct and not required to be reordered.");

  b.problem("modelx3", "State a rational-function domain", "Find the domain of (x+1)/(x-4).");
  r = b.step("modelx3", "State the restriction.", "all real numbers", "algebra");
  b.hint("modelx3", "h1", "Exclude a zero denominator", "Solve x-4=0.");
  b.defect("modelx3", "missing_domain_exclusion", [accepted(`E${r}`, ["x!=4", "(-infinity,4)U(4,infinity)"])], "x=4 is excluded.");

  b.problem("modelx4", "Make a piecewise function continuous", "Let f(x)=x+2 for x<2 and f(x)=k for x>=2. Find k.");
  b.step("modelx4", "State k.", "4", "numeric");
  b.hint("modelx4", "h1", "Match the boundary values", "The left-hand value approaches 4.");
  b.clean("modelx4", "The continuity value is correct.");

  b.problem("modelx5", "Find a doubling time", "A quantity follows P(t)=100*(1.08)**t. Find the exact doubling time.");
  r = b.step("modelx5", "Give an exact expression for t.", "8.66", "numeric");
  b.hint("modelx5", "h1", "Set the doubled value", "Solve 200=100*(1.08)**t.");
  b.hint("modelx5", "h2", "Use logarithms", "Take logarithms after dividing by 100.", "h1");
  b.defect("modelx5", "wrong_growth_time_and_form", [exact(`E${r}`, "log(2)/log(1.08)"), exact(`F${r}`, "algebra")], "The exact logarithmic expression is required.");

  b.problem("modelx6", "Find an inverse function", "For f(x)=3*x+5, find f**(-1)(x).");
  b.step("modelx6", "State the inverse.", "(x-5)/3", "algebra");
  b.hint("modelx6", "h1", "Swap variables", "Write x=3*y+5 and solve for y.");
  b.clean("modelx6", "The inverse is correct.");

  b.problem("modelx7", "Convert a speed", "Convert 72 kilometers per hour to meters per second.");
  r = b.step("modelx7", "State the speed in m/s.", "24", "numeric");
  b.hint("modelx7", "h1", "Use both conversion factors", "Multiply by 1000/3600.");
  b.defect("modelx7", "unit_conversion_error", [exact(`E${r}`, "20")], "72*1000/3600=20.");

  b.problem("modelx8", "Identify a non-solution", "For (x+1)/(x-2)=3, select the value that cannot be a solution because it is outside the domain.");
  r = b.step("modelx8", "Select the excluded value.", "3.5", "mc", "2|3.5|-1|0");
  b.hint("modelx8", "h1", "Inspect the denominator", "The denominator vanishes when x=2.");
  b.defect("modelx8", "semantic_mc_wrong_answer", [exact(`E${r}`, "2")], "The question asks for the excluded value, not the equation's solution.");

  b.problem("modelx9", "Use an arithmetic sequence", "An arithmetic sequence has a1=7 and d=4. Find a20.");
  b.step("modelx9", "State a20.", "83", "numeric");
  b.hint("modelx9", "h1", "Use the nth-term formula", "Compute 7+(20-1)*4.");
  b.clean("modelx9", "The arithmetic-sequence value is correct.");

  b.problem("modelx10", "Use a geometric sequence", "A geometric sequence has a1=3 and r=2. Find a6.");
  r = b.step("modelx10", "State a6.", "48", "numeric");
  b.hint("modelx10", "h1", "Use n-1 factors", "Compute 3*2**(6-1).");
  b.defect("modelx10", "geometric_index_error", [exact(`E${r}`, "96")], "The exponent is 5, not 4.");

  b.problem("modelx11", "Compute a probability without replacement", "A bag has 5 red and 4 blue balls. Find the probability of red then blue without replacement.");
  b.step("modelx11", "Give the exact probability.", "5/18", "algebra");
  b.hint("modelx11", "h1", "Multiply conditional probabilities", "Use (5/9)*(4/8).");
  b.clean("modelx11", "The exact probability is correct.");

  b.problem("modelx12", "Solve a depreciation model", "A machine worth 20000 loses 15% each year. Find its value after 2 years.");
  b.step("modelx12", "State the value.", "14450", "numeric");
  r = b.hint("modelx12", "h1", "Use the retention factor", "Multiply by 0.85 twice.");
  b.rows[r - 1][0] = null;
  b.defect("modelx12", "missing_problem_name_inside_block", [exact(`A${r}`, "modelx12")], "Every non-empty row belongs to a named problem block.");

  b.problem("modelx13", "Analyze two model outputs", "Evaluate a linear model and then compare it with an exponential model.");
  b.step("modelx13", "Evaluate L(3)=4*3+1.", "13", "numeric");
  b.hint("modelx13", "h1", "Substitute into L", "Use x=3.");
  b.hint("modelx13", "h2", "Simplify", "Compute 12+1.", "h1");
  b.step("modelx13", "Evaluate E(3)=2**3.", "8", "numeric");
  r = b.hint("modelx13", "h3", "Use the exponent", "Compute 2*2*2.", "h2");
  b.defect("modelx13", "continuous_dependency_crosses_step", [exact(`H${r}`, null)], "A new step restarts the dependency chain even when labels continue.");

  b.problem("modelx14", "Evaluate a left-hand limit", "Let f(x)=x**2 for x<2. Find the left-hand limit at x=2.");
  b.step("modelx14", "State the limit as an equation.", "lim_(x->2-)f(x)=4", "algebra");
  b.hint("modelx14", "h1", "Use the left branch", "Substitute values approaching 2 into x**2.");
  b.clean("modelx14", "The full equation is a correct answer representation and should not be normalized to 4.");

  b.problem("modelx15", "Identify a vertical asymptote", "For r(x)=(2*x+1)/(x-2), find the vertical asymptote.");
  r = b.step("modelx15", "State the vertical line.", "x=2 (vertical)", "algebra");
  b.hint("modelx15", "h1", "Set the denominator to zero", "Solve x-2=0.");
  b.defect("modelx15", "descriptive_label_in_graded_answer", [exact(`E${r}`, "x=2")], "The graded field contains only the equation, not a label.");
  return b;
}

function mixedBook() {
  const b = new HeldOutBook("heldout-04-mixed-advanced.xlsx", {
    notation: "latex",
    dependencyConvention: "reset_per_step",
    topic: "Mixed Precalculus",
  });

  b.problem("mixq1", "Write a circle equation", "A circle has center $$(2,-1)$$ and passes through $$(5,3)$$.");
  let r1 = b.step("mixq1", "Find the squared radius.", "16", "numeric");
  b.hint("mixq1", "h1", "Use the distance formula", "The coordinate differences are 3 and 4.");
  let r2 = b.step("mixq1", "Write the circle equation.", "$$(x-2)^2+(y+1)^2=16$$", "algebra");
  b.hint("mixq1", "h1", "Use standard form", "Place the squared radius on the right.");
  b.defect("mixq1", "linked_radius_error", [exact(`E${r1}`, "25"), exact(`E${r2}`, "$$(x-2)^2+(y+1)^2=25$$")], "A 3-4-5 triangle has squared radius 25, and the final equation must agree.");

  b.problem("mixq2", "Solve a quadratic exactly", "Solve $$2x^2-4x-3=0$$.");
  b.step("mixq2", "List both roots.", "$$x=1-\\frac{\\sqrt{10}}{2},1+\\frac{\\sqrt{10}}{2}$$", "algebra");
  b.hint("mixq2", "h1", "Use the quadratic formula", "The discriminant is 40.");
  b.clean("mixq2", "Both exact roots are present; ordering is not a defect.");

  b.problem("mixq3", "Check a composition", "Let $$f(x)=3x-5$$ and $$f^{-1}(x)=\\frac{x+5}{3}$$.");
  b.step("mixq3", "Verify the inverse.", "$$x$$", "algebra");
  b.hint("mixq3", "h1", "Substitute", "Place $$\\frac{x+5}{3}$$ into $$3x-5$$.");
  const shifted = blankRow();
  shifted[1] = "mixq3";
  shifted[2] = "hint";
  shifted[3] = "Simplify the composition";
  shifted[4] = "Cancel the factor of 3, then subtract 5.";
  shifted[7] = "h2";
  shifted[8] = "h1";
  const r = b.addRaw(shifted);
  b.defect("mixq3", "row_shift_right_one_column", [
    exact(`A${r}`, "mixq3"), exact(`B${r}`, "hint"), exact(`C${r}`, "Simplify the composition"),
    exact(`D${r}`, "Cancel the factor of 3, then subtract 5."), exact(`E${r}`, null),
    exact(`G${r}`, "h2"), exact(`H${r}`, "h1"), exact(`I${r}`, null),
  ], "The content row is displaced one column right and must be restored as a unit.");

  b.problem("mixq4", "Compute a conditional probability", "A class has 8 seniors and 10 juniors. Two students are chosen without replacement. Find P(senior then junior).");
  b.step("mixq4", "Give the exact probability.", "$$\\frac{40}{153}$$", "algebra");
  b.hint("mixq4", "h1", "Multiply in order", "Use $$\\frac{8}{18}\\cdot\\frac{10}{17}$$.");
  b.clean("mixq4", "The unsimplified product reduces to 40/153.");

  b.problem("mixq5", "Keep an exact fraction", "State one half as an exact fraction.");
  r1 = b.step("mixq5", "Give the fraction.", new Date("2026-01-02T00:00:00Z"), "algebra");
  b.hint("mixq5", "h1", "Use numerator over denominator", "The numerator is 1 and denominator is 2.");
  b.defect("mixq5", "excel_date_coercion", [accepted(`E${r1}`, ["1/2", "$$\\frac{1}{2}$$"])], "Excel coerced the fraction to a date value.");

  b.problem("mixq6", "Use a logarithm law", "Expand $$\\log_b(xy^2)$$.");
  b.step("mixq6", "Give the expanded form.", "$$\\log_b(x)+2\\log_b(y)$$", "algebra");
  b.hint("mixq6", "h1", "Use product and power laws", "Separate the product before bringing down the exponent.");
  b.clean("mixq6", "The expansion is correct.");

  b.problem("mixq7", "Classify an equation answer", "Solve $$5t-4=11$$ and state the equation for t.");
  r1 = b.step("mixq7", "State the solved equation.", "$$t=3$$", "numeric");
  b.hint("mixq7", "h1", "Add 4 and divide by 5", "The result is $$t=3$$.");
  b.defect("mixq7", "answer_type_semantics", [exact(`F${r1}`, "algebra")], "An explicit equation in a variable is algebraic.");

  b.problem("mixq8", "Solve two independent steps", "Find a midpoint, then find a slope.");
  b.step("mixq8", "Find the midpoint of (1,2) and (5,8).", "$$(3,5)$$", "algebra");
  b.hint("mixq8", "h1", "Average coordinates", "Average x-values and y-values separately.");
  b.hint("mixq8", "h2", "Check the pair", "The averages are 3 and 5.", "h1");
  b.step("mixq8", "Find the slope through the same points.", "$$\\frac{3}{2}$$", "algebra");
  r1 = b.hint("mixq8", "h1", "Use rise over run", "Compute $$(8-2)/(5-1)$$.", "h2");
  b.defect("mixq8", "reset_step_first_hint_dependency", [exact(`H${r1}`, null)], "The reset-per-step chain starts again at h1 with no dependency.");

  b.problem("mixq9", "Choose a function value", "For $$f(x)=x^2-1$$, choose $$f(3)$$.");
  b.step("mixq9", "Select the value.", "8", "mc", "9|6|7|8");
  b.hint("mixq9", "h1", "Substitute", "Compute $$3^2-1$$.");
  b.clean("mixq9", "The correct choice is present exactly once and need not be first.");

  b.problem("mixq10", "Follow one hint chain", "Solve $$4x+1=13$$.");
  b.step("mixq10", "Find x.", "3", "numeric");
  b.hint("mixq10", "h1", "Subtract 1", "Obtain $$4x=12$$.");
  r1 = b.hint("mixq10", "h1", "Divide by 4", "Obtain $$x=3$$.", "h1");
  b.defect("mixq10", "duplicate_identifier_within_step", [exact(`G${r1}`, "h2")], "Identifiers must be unique within one step chain.");

  b.problem("mixq11", "Use a hint without revealing the result", "Solve $$3z=36$$.");
  b.step("mixq11", "Find z.", "12", "numeric");
  r1 = b.hint("mixq11", "h1", "Undo multiplication", "The answer is 12.");
  b.defect("mixq11", "answer_giveaway_hint", [exact(`D${r1}`, "Divide both sides by 3.")], "A hint should teach the operation rather than state the final answer.");

  b.problem("mixq12", "Recognize an equivalent identity", "Simplify $$2\\sin(x)\\cos(x)$$.");
  b.step("mixq12", "Give an equivalent expression.", "$$\\sin(2x)$$", "algebra");
  b.hint("mixq12", "h1", "Use the sine double angle", "Match the identity directly.");
  b.clean("mixq12", "The expression is already equivalent and correct.");

  b.problem("mixq13", "State an interval exactly", "Solve $$(x+1)(x-2)>0$$.");
  r1 = b.step("mixq13", "Give the solution interval.", "$$(-\\infty,-1)\\cup(2,\\infty)$$ ", "algebra");
  b.hint("mixq13", "h1", "Use a sign chart", "The product is positive outside the roots.");
  b.defect("mixq13", "trailing_space_exact_answer", [exact(`E${r1}`, "$$(-\\infty,-1)\\cup(2,\\infty)$$")], "Boundary whitespace changes the exact graded value.");

  b.problem("mixq14", "Find a function value", "For $$p(x)=x^3-2x$$, find $$p(2)$$.");
  b.step("mixq14", "Evaluate p(2).", "4", "numeric");
  r1 = b.hint("mixq14", "h1", "Substitute 2", "Compute $$2^3-2(2)$$.");
  b.rows[r1 - 1][0] = null;
  b.defect("mixq14", "missing_problem_name", [exact(`A${r1}`, "mixq14")], "Every content row needs its block's Problem Name.");

  b.problem("mixq15", "Solve a system and verify the operations", "Solve $$2x+y=9$$ and $$x-y=0$$.");
  b.step("mixq15", "State x.", "2", "numeric");
  r1 = b.hint("mixq15", "h1", "Use x=y", "Substitute y=x to obtain 3x=9.");
  r2 = b.hint("mixq15", "h2", "Divide", "Divide 3x=9 by 2.", "h1");
  b.defect("mixq15", "linked_system_answer_and_hint", [exact(`E${r1 - 1}`, "3"), exact(`D${r2}`, "Divide 3x=9 by 3.")], "The substitution gives x=3, and the hint must divide by 3.");
  return b;
}

async function writeBook(book, outputDir) {
  const workbook = Workbook.create();
  const sheet = workbook.worksheets.add("Problems");
  sheet.getRange(`A1:P${book.rows.length}`).values = book.rows;
  sheet.getRange(`A1:P${book.rows.length}`).format.wrapText = false;
  sheet.getRange(`A1:P${book.rows.length}`).format.rowHeight = 15;
  sheet.getRange("A1:P1").format.font = { bold: true };
  const widths = [22, 12, 39, 64, 38, 13, 12, 15, 70, 22, 15, 48, 38, 26, 18, 16];
  widths.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, book.rows.length, 1).format.columnWidth = width;
  });
  sheet.freezePanes.freezeRows(1);

  const inspection = await workbook.inspect({
    kind: "table",
    range: `Problems!A1:P${Math.min(book.rows.length, 16)}`,
    include: "values,formulas",
    tableMaxRows: 16,
    tableMaxCols: 16,
    maxChars: 5000,
  });
  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 50 },
    summary: `${book.filename} formula error scan`,
  });

  const preview = await workbook.render({
    sheetName: "Problems",
    range: `A1:P${book.rows.length}`,
    scale: 0.55,
    format: "png",
  });
  await fs.writeFile(
    path.join(outputDir, `${path.parse(book.filename).name}.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(path.join(outputDir, book.filename));
  await fs.writeFile(
    path.join(outputDir, "evaluation-keys", `${path.parse(book.filename).name}.key.json`),
    `${JSON.stringify(book.key(), null, 2)}\n`,
    "utf8",
  );
  return {
    file: book.filename,
    rows: book.rows.length,
    problems: 15,
    defectGroups: book.defectGroups.length,
    automatedChecks: book.defectGroups.reduce((total, group) => total + group.corrections.length, 0),
    cleanControls: book.cleanControls.length,
    inspection: inspection.ndjson.slice(0, 800),
    formulaErrors: formulaErrors.ndjson,
  };
}

const outputDir = process.argv[2];
if (!outputDir) throw new Error("usage: build_heldout_deployment_suite.mjs OUTPUT_DIR");
await fs.mkdir(path.join(outputDir, "evaluation-keys"), { recursive: true });

const books = [algebraBook(), trigBook(), modelingBook(), mixedBook()];
const summary = [];
for (const book of books) summary.push(await writeBook(book, outputDir));
await fs.writeFile(
  path.join(outputDir, "manifest.json"),
  `${JSON.stringify({ generatedAt: new Date().toISOString(), books: summary }, null, 2)}\n`,
  "utf8",
);
console.log(JSON.stringify(summary, null, 2));
