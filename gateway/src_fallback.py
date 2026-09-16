import re
TAGS=['cinematic','commercial','portrait','product','landscape','slow','dynamic','studio','dramatic','natural','luxury','minimal','anime','documentary','fashion','travel','food','realistic','editorial','social','vertical','widescreen']
def classify(text):
 t=str(text).strip(); l=t.lower(); typ='video'
 if re.search(r'text\s*(to|→)\s*image|generate.*image|image generation',l): typ='text-to-image'
 elif re.search(r'image\s*(to|→)\s*video|animate.*image',l): typ='image-to-video'
 elif re.search(r'video\s*(to|→)\s*video|restyle.*video|edit.*video',l): typ='video-to-video'
 elif re.search(r'avatar|talking head|digital human',l): typ='avatar'
 elif re.search(r'ad|advert|commercial|campaign',l): typ='commercial'
 elif re.search(r'caption|subtitle|transcri',l): typ='caption'
 risks=[]
 if re.search(r'password|api key|secret|private key|token\b',l): risks.append('credential_request')
 if re.search(r'bypass|exploit|malware|ransomware|steal|phish',l): risks.append('abuse_request')
 if re.search(r'copyright|pirated|torrent|remove watermark',l): risks.append('copyright_risk')
 return {'type':typ,'tags':[x for x in TAGS if x in l][:8],'riskFlags':risks,'summary':t[:500]}
