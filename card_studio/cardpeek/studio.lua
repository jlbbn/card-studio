local select_apdu = os.getenv("CARD_STUDIO_SELECT_APDU")
local apdu = os.getenv("CARD_STUDIO_APDU")

local function finish()
  pcall(function() card.disconnect() end)
end

if not select_apdu or not select_apdu:match("^[0-9A-F]+$") or #select_apdu < 10 or #select_apdu % 2 ~= 0 then
  print('{"error":"INVALID_APDU"}')
  return
end
if not apdu or not apdu:match("^[0-9A-F]+$") or #apdu < 10 or #apdu % 2 ~= 0 then
  print('{"error":"INVALID_APDU"}')
  return
end

local ok = pcall(function()
  if not card.connect() then
    print('{"error":"NO_CARD"}')
    return
  end

  local sw = card.send(bytes.new(8, select_apdu))
  if sw ~= 0x9000 then
    print(string.format('{"error":"SELECT_FAILED","sw":%d}', sw))
    finish()
    return
  end

  local response
  sw, response = card.send(bytes.new(8, apdu))
  local data = ""
  if response then data = tostring(response):gsub("[^0-9A-Fa-f]", "") end
  print(string.format('{"sw":%d,"data":"%s"}', sw, data))
  finish()
end)

if not ok then
  finish()
  print('{"error":"CARDPEEK_ERROR"}')
end
