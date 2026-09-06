import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const root = path.resolve("outputs/day1-day2-live-20260905");
const stems = [
  "sealed-a-algebra",
  "sealed-b-trig-logs",
  "sealed-c-matrices-conics",
  "sealed-d-sequences-probability",
];

for (const stem of stems) {
  const filename = path.join(root, stem, "corrected.xlsx");
  const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(filename));
  const table = await workbook.inspect({
    kind: "table",
    range: "Problems!A1:P60",
    include: "values,formulas",
    tableMaxRows: 60,
    tableMaxCols: 16,
    tableMaxCellChars: 120,
    maxChars: 20000,
  });
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
    options: { useRegex: true, maxResults: 300 },
    summary: "final formula error scan",
  });
  const preview = await workbook.render({
    sheetName: "Problems",
    range: "A1:I30",
    scale: 1,
    format: "png",
  });
  const previewPath = path.join(root, stem, "corrected-preview.png");
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
  console.log(JSON.stringify({
    stem,
    inspectedBytes: table.ndjson.length,
    formulaErrorScan: errors.ndjson,
    previewPath,
  }));
}
