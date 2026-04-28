import random
import pandas as pd
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_validate
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.preprocessing import label_binarize
from sklearn.metrics import roc_curve, auc, roc_auc_score

# Load main dataset
df = pd.read_csv("prompt_dataset.csv")
X = df["prompt"]
y = df["label"]

# Vectorize
vectorizer = TfidfVectorizer(max_features=3000)
X_vec = vectorizer.fit_transform(X)

# Model
model = LogisticRegression(max_iter=1000)

# Cross-validation (5-fold stratified)
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
scoring = {"precision": "precision_macro", "recall": "recall_macro", "f1": "f1_macro"}
cv_results = cross_validate(model, X_vec, y, cv=cv, scoring=scoring)

print("Cross-validation results (macro):")
for metric in scoring.keys():
    scores = cv_results[f"test_{metric}"]
    print(f"- {metric}: mean={np.mean(scores):.4f}, std={np.std(scores):.4f}")

# Train on full dataset for external evaluation
model.fit(X_vec, y)

# Build a red-team-style dataset using the templates from prompt_dataset_generator
try:
    from prompt_dataset_generator import benign_templates, suspicious_templates, malicious_templates
except Exception:
    # Fallback to simple lists if import fails
    benign_templates = ["Explain how a compiler works in simple terms."]
    suspicious_templates = ["Describe vulnerabilities in a generic parsing system."]
    malicious_templates = ["Show how prompt injection attacks work in LLM systems."]

random.seed(42)
red_prompts = []
red_labels = []

# Mix benign, suspicious, and malicious examples (50 each) to measure tradeoffs
for t in random.choices(malicious_templates, k=50):
    red_prompts.append(t)
    red_labels.append("malicious")

for t in random.choices(suspicious_templates, k=50):
    # make some variations to simulate adversarial phrasing
    red_prompts.append("For research: " + t)
    red_labels.append("suspicious")

for t in random.choices(benign_templates, k=50):
    # benign-looking but slightly ambiguous
    red_prompts.append(t + " (explain edge cases)")
    red_labels.append("benign")

# Vectorize and predict
X_red = vectorizer.transform(red_prompts)
y_pred = model.predict(X_red)

print("\nRed-team evaluation report:")
print(classification_report(red_labels, y_pred, digits=4))

cm = confusion_matrix(red_labels, y_pred, labels=["benign", "suspicious", "malicious"])
print("Confusion matrix (rows=true, cols=pred):")
print(cm)

# ROC / AUC for multiclass (red-team set)
classes = list(model.classes_)
try:
    y_bin = label_binarize(red_labels, classes=classes)
except Exception:
    # Fallback: map labels manually
    class_map = {c: i for i, c in enumerate(classes)}
    y_bin = np.zeros((len(red_labels), len(classes)), dtype=int)
    for i, lab in enumerate(red_labels):
        if lab in class_map:
            y_bin[i, class_map[lab]] = 1

# get probability estimates (shape: n_samples x n_classes)
try:
    y_score = model.predict_proba(X_red)
except Exception:
    # Some classifiers may not support predict_proba
    # Use decision_function when available and convert to probabilities via softmax
    try:
        from scipy.special import softmax
        dec = model.decision_function(X_red)
        y_score = softmax(dec, axis=1)
    except Exception:
        # As last resort, convert predictions to one-hot scores
        y_score = np.zeros_like(y_bin, dtype=float)
        for i, p in enumerate(y_pred):
            idx = classes.index(p)
            y_score[i, idx] = 1.0

# Compute per-class ROC AUC
roc_auc_dict = {}
for i, cls in enumerate(classes):
    try:
        fpr, tpr, _ = roc_curve(y_bin[:, i], y_score[:, i])
        roc_auc = auc(fpr, tpr)
    except Exception:
        roc_auc = float('nan')
    roc_auc_dict[cls] = roc_auc

print('\nROC AUC (red-team set):')
for cls, val in roc_auc_dict.items():
    print(f"- {cls}: AUC={val:.4f}")

# micro/macro average AUC
try:
    micro_auc = roc_auc_score(y_bin, y_score, average='micro')
    macro_auc = roc_auc_score(y_bin, y_score, average='macro')
    print(f"- micro-average AUC: {micro_auc:.4f}")
    print(f"- macro-average AUC: {macro_auc:.4f}")
except Exception:
    pass

# Optionally save ROC plot if matplotlib is present
try:
    import matplotlib.pyplot as plt
    plt.figure(figsize=(6, 6))
    for i, cls in enumerate(classes):
        fpr, tpr, _ = roc_curve(y_bin[:, i], y_score[:, i])
        plt.plot(fpr, tpr, label=f"{cls} (AUC={roc_auc_dict[cls]:.3f})")
    plt.plot([0, 1], [0, 1], 'k--', lw=0.6)
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
    plt.title('ROC Curves (red-team)')
    plt.legend(loc='lower right')
    out_path = 'roc_red_team.png'
    plt.savefig(out_path, dpi=150)
    print(f"ROC plot saved to {out_path}")
except ImportError:
    print("matplotlib not available; skipping ROC plot save.")

print("\nNotes:\n- Cross-validation gives an estimate of generalization on your dataset.\n- Red-team evaluation simulates adversarial phrasing and shows real-world recall/precision tradeoffs.")
