on open location theURL
    try
        set pathStart to (offset of "?path=" in theURL) + 6
        if pathStart < 7 then return
        set encodedPath to text pathStart through -1 of theURL
        set filePath to do shell script "python3 -c \"import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))\" " & quoted form of encodedPath
        do shell script "open -b com.adobe.Photoshop " & quoted form of filePath
    end try
end open location
