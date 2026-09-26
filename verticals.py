"""Per-trade wording + look. Add a new trade by copying one block."""
V = {
 "restaurant": {"label": "مطعم", "font": "Lalezar",
   "colors": {"bg": "#FBEFE2", "ink": "#2A1A12", "accent": "#B3311E", "soft": "#F2D9BF", "muted": "#7A5B48"},
   "status": {"received": "وصلنا طلبك", "preparing": "طلبك على النار",
              "out": "طلبك طلع ويّا المندوب", "delivered": "بالعافية، وصل طلبك", "cancelled": "انلغى الطلب"}},
 "grocery": {"label": "مواد غذائية", "font": "Readex Pro",
   "colors": {"bg": "#EEF2E6", "ink": "#1F2A1A", "accent": "#5B3A1E", "soft": "#DCE5CC", "muted": "#5E6B52"},
   "status": {"received": "وصلنا طلبك", "preparing": "دنجهّز طلبك",
              "out": "طلبك بالطريق إلك", "delivered": "تم التسليم، تسلم", "cancelled": "انلغى الطلب"}},
 "pharmacy": {"label": "صيدلية", "font": "Noto Kufi Arabic",
   "colors": {"bg": "#EEF6F5", "ink": "#12302F", "accent": "#0F6E6A", "soft": "#D3E9E6", "muted": "#4F6B69"},
   "status": {"received": "استلمنا طلبك", "preparing": "الصيدلي يجهّز طلبك",
              "out": "طلبك بالطريق", "delivered": "تم التسليم، سلامتك", "cancelled": "انلغى الطلب"}},
 "perfume": {"label": "عطور", "font": "Amiri",
   "colors": {"bg": "#F6EFF3", "ink": "#2B1B2E", "accent": "#8C6A1F", "soft": "#EADCE4", "muted": "#6E5A70"},
   "status": {"received": "استلمنا طلبك", "preparing": "نغلّف طلبك بعناية",
              "out": "طلبك بالطريق", "delivered": "وصل طلبك، يعطيك العافية", "cancelled": "انلغى الطلب"}},
 "wholesale": {"label": "جملة مواد غذائية", "font": "Reem Kufi",
   "colors": {"bg": "#F3F3F1", "ink": "#16325F", "accent": "#1F4E9C", "soft": "#DCE3EE", "muted": "#55607A"},
   "unit_word": "كيس",
   "status": {"received": "وصلنا طلبك", "preparing": "نجهّز الحمولة",
              "out": "الحمولة بالطريق إلك", "delivered": "تم التسليم، تسلم", "cancelled": "انلغى الطلب"}},
 "clothing": {"label": "ملابس", "font": "Readex Pro",
   "colors": {"bg": "#F1F2F8", "ink": "#1B2140", "accent": "#26356B", "soft": "#DFE2F0", "muted": "#5A6080"},
   "status": {"received": "استلمنا طلبك", "preparing": "نجهّز قطعك",
              "out": "طلبك بالطريق", "delivered": "وصل طلبك، تتهنى بيه", "cancelled": "انلغى الطلب"}},
}
FLOW = ["received", "preparing", "out", "delivered"]
STEP_NAMES = {"received": "الاستلام", "preparing": "التجهيز", "out": "التوصيل", "delivered": "التسليم"}

def get(vertical):
    return V.get(vertical, V["restaurant"])
