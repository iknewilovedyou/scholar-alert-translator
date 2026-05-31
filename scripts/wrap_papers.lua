-- pandoc Lua filter: wrap each paper (content between ## headers) in tcolorbox papercard
-- Used by Style B and Style C

local paper_blocks = {}
local in_paper = false

function Pandoc(doc)
  local new_blocks = {}

  for _, blk in ipairs(doc.blocks) do
    if blk.t == "Header" and blk.level == 2 then
      -- Close previous paper if one is open
      if in_paper and #paper_blocks > 0 then
        table.insert(paper_blocks, pandoc.RawBlock("latex", "\\end{papercard}"))
        for _, pb in ipairs(paper_blocks) do
          table.insert(new_blocks, pb)
        end
        paper_blocks = {}
      end
      in_paper = true
      table.insert(paper_blocks, pandoc.RawBlock("latex", "\\begin{papercard}"))
      table.insert(paper_blocks, blk)
    elseif in_paper then
      table.insert(paper_blocks, blk)
    else
      table.insert(new_blocks, blk)
    end
  end

  -- Close the last paper
  if in_paper and #paper_blocks > 0 then
    table.insert(paper_blocks, pandoc.RawBlock("latex", "\\end{papercard}"))
    for _, pb in ipairs(paper_blocks) do
      table.insert(new_blocks, pb)
    end
  end

  doc.blocks = new_blocks
  return doc
end
