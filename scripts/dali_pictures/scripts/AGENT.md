Find product pictures for new DALI devices. All paths are relative to the dali_pictures folder, run commands from it. Python only, never node.

Your items are in new/queue/batch_NNN.json, the batch number is given in your task. Fields: gtin (the key, without leading zeros), gtins (all spellings), brand, product, optionally part (the manufacturer's part number, often a better search key than the product name), again + rejected_comment (the user rejected the previous picture, the comment says why). Skip an item if new/results/<gtin>.json already exists with a status other than "unsearched", unless the item has "again": true (check each file directly with `test -f` or python, do not rely on a truncated directory listing).

Procedure for one item:
1. Find the product page by brand and model (or part number) with WebSearch. The manufacturer site first, then a big distributor.
2. Search results rarely contain image URLs. Open the product page with WebFetch and ask it for the main product image URL (og:image, the gallery img src, data-src). Many sites build image URLs from the article number, so one found URL often gives the pattern for the rest of the batch.
3. Download and validate ONLY with this command: `python3 scripts/pictures.py check <image-url> <gtin>`. Never curl/wget. It saves new/images/<gtin>.<ext> on success, rejects a dark or colored background and placeholders, passes a light gray one with "light_background": true (normalization whitens it; prefer a white one when both exist), and marks a picture under 600 px as "small".
4. Look at what you accept: a different product, a drawing, a collage, several views in one picture, a watermark or a placeholder → delete the saved file and keep looking. Text and logos next to the product are fine, the review script crops them.
5. Prefer the biggest plain product photo on white. If nothing of 600 px or more exists, take the best smaller one, the user confirms it on the review page.

Rules:
- Load WebSearch and WebFetch with ToolSearch "select:WebSearch,WebFetch". At most 3 searches and 6 checked images per item, then status not_found. Reuse one catalog page for several items when possible.
- A same-series sibling only if it looks the same (same housing); exact_model false.
- For an item with "again": true, do not offer the same picture URL again; the new result overwrites the old one.
- If web search stops working (quota error), do NOT write not_found for the unsearched items: write status "unsearched" for them, and stop.
- Write new/results/<gtin>.json for every item: gtin, brand, product, status (ok | not_found | unsearched), url, page, exact_model, note, tries (the check JSON lines). Write the JSON files with the Write tool or a python one-liner, never with shell heredocs (`cat > file <<EOF` hangs).
- Never run kill, pkill or killall: other agents work in parallel on this machine.
- Touch nothing outside new/images and new/results.

Finish with one line: counts per status, plus GTINs that need a visual check (sibling photos, doubts).
